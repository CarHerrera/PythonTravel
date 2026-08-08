import asyncio
import datetime
import glob
import json
import os
import re
import unicodedata
from playwright.async_api import async_playwright

from scraping_common import (
    finish_atomic_write,
    human_pause,
    install_process_guards,
    start_atomic_write,
)

URL = "https://www.barcelo.com/en-us/"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "data")

DEPARTURE_DATE = "10/31"
RETURN_DATE = "11/07"
CHECK_IN_MONTH_YEAR = "October 2026"
CHECK_IN_DAY = "31"
CHECK_OUT_MONTH_YEAR = "November 2026"
CHECK_OUT_DAY = "7"
OUTPUT_PATH = os.path.join(DATA_DIR, "rates_barcelo_10-31.jsonl")
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "barcelo_debug_out")

# Southwest decorates Barcelo names with "All Inclusive"/"Adults Only" suffixes
# Barcelo's own destination panel doesn't have — stripped before comparing.
# Accents are folded too (Barceló/Bávaro vs Barcelo/Bavaro).
DECORATION_RE = re.compile(r"\s*-?\s*(all inclusive|adults only|ai)\b", re.IGNORECASE)

# Room size is reported in square meters directly ("45 m2") — no sq-ft
# conversion needed, unlike RIU/Palladium/Iberostar.
SIZE_SQM_RE = re.compile(r"(\d+)\s*m2", re.IGNORECASE)


def normalize_hotel_name(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = DECORATION_RE.sub("", name)
    return re.sub(r"\s+", " ", name).strip().lower()


def match_southwest_name(candidate_name, southwest_names):
    normalized_to_original = {normalize_hotel_name(n): n for n in southwest_names}
    return normalized_to_original.get(normalize_hotel_name(candidate_name))


def load_barcelo_hotels_by_destination():
    by_destination = {}
    for path in glob.glob(os.path.join(DATA_DIR, "hotels_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                hotel = json.loads(line)
                folded = unicodedata.normalize("NFKD", hotel["name"]).encode("ascii", "ignore").decode("ascii")
                if re.search(r"\bbarcel[o]?\b", folded, re.IGNORECASE):
                    by_destination.setdefault(hotel["destination"], []).append(hotel["name"])
    return by_destination


async def fetch_directory(page):
    """The destination search box's own panel already lists every Barcelo
    hotel worldwide (confirmed live: ~190 names, unfiltered regardless of
    what's typed into the box — typing doesn't narrow it at all here, unlike
    Iberostar's live-filtered panel) — read directly off the DOM instead of
    typing/searching per hotel."""
    await page.goto(URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    try:
        await page.locator("#didomi-notice-agree-button").click(timeout=3000)
        await page.wait_for_timeout(500)
    except Exception:
        pass  # cookie banner already dismissed / didn't appear this time

    dest_input = page.locator("#destination-fb")
    await dest_input.click(timeout=10000)
    await page.wait_for_timeout(1000)
    names = await page.locator("#destination-popover .js-title-hotel").all_inner_texts()
    return [n.strip() for n in names if n.strip()]


def resolve_barcelo_hotels(by_destination, directory_names):
    resolved = []
    for southwest_destination, hotel_names in by_destination.items():
        for name in hotel_names:
            matched_title = match_southwest_name(name, directory_names)
            if matched_title is None:
                print(f"WARNING: {name!r} not found in Barcelo's own hotel directory — skipping")
                continue
            resolved.append((southwest_destination, name, matched_title))
    return resolved


async def click_month_day(page, next_btn, month_year, day_text):
    """Clicks a day cell in Barcelo's two-pane datepicker. Confirmed live
    the day cells' own "time" attribute (an epoch-ms timestamp) shifts by
    an hour across the Nov 2026 DST boundary, so matching by that value
    directly is unsafe — this instead locates the correct pane by its own
    visible month title (clicking "next" until it appears, capped, same
    self-correcting approach as riu_script.py/palladium_script.py/
    iberostar_script.py's identical date-drift fix) and clicks the cell by
    its visible day number within that pane specifically."""
    idx = None
    for _ in range(10):
        titles = [t.replace("\n", " ").strip() for t in await page.locator(".datepicker__month-name").all_inner_texts()]
        if month_year in titles:
            idx = titles.index(month_year)
            break
        await next_btn.click()
        await page.wait_for_timeout(400)
    else:
        raise RuntimeError(f"{month_year} never appeared in the calendar after 10 next-month clicks")
    pane = page.locator('table[id^="month-"]').nth(idx)
    cell = pane.locator("td.datepicker__month-day--visibleMonth", has_text=day_text).first
    # force=True: confirmed live the resolved cell is correct (right day,
    # right pane) but a neighboring cell/row intercepts the click — same
    # incidental-visual-overlap class already fixed the same way in
    # riu_script.py/iberostar_script.py, not a case needing a genuine
    # interaction-triggered side effect blocked by force.
    await cell.click(force=True)


def _parse_price(text):
    if not text:
        return None
    cleaned = text.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


async def parse_barcelo_room_list(page):
    """Confirmed live: once a search completes, the hotel's own page
    (barcelo.com/en-us/<slug>/) shows every room type with per-night pricing
    already applied for the searched dates — no further per-room navigation
    needed, unlike RIU/Palladium/Iberostar. Only ever shows one ("From")
    rate per room, not multiple board options."""
    rooms = []
    room_els = page.locator(".c-hotel-room-list-JS")
    room_count = await room_els.count()
    for i in range(room_count):
        room = room_els.nth(i)
        try:
            name_el = room.locator(".c-hotel-room-list__body-title")
            room_name = (await name_el.first.inner_text()).strip() if await name_el.count() else ""

            # The desktop/mobile duplicate pair's "hidden" class isn't a
            # reliable signal of which one is genuinely rendered (confirmed
            # live: sometimes BOTH carry "hidden" in the static markup,
            # apparently a responsive-breakpoint artifact rather than
            # per-instance visibility) — same failure class as the
            # innerText/textContent lesson from palladium_script.py's
            # amenities bug. Reading textContent for every match and taking
            # the first non-empty one sidesteps the ambiguity rather than
            # trying to guess which one is "the real one".
            size_texts = await room.locator(".c-hotel-room-list__body-room-size").evaluate_all(
                "els => els.map(e => e.textContent || '')"
            )
            size_sq_m = None
            for t in size_texts:
                m = SIZE_SQM_RE.search(t)
                if m:
                    size_sq_m = int(m.group(1))
                    break

            amenities = [a.strip() for a in await room.locator(".c-hotel-room-list__body-list-ul li").all_inner_texts() if a.strip()]

            # The <img> tags themselves are lazy-loaded (loading="lazy",
            # no src set until scrolled into view — confirmed live every
            # room past the first ended up with an empty src, since nothing
            # in this flow ever scrolls the page). The real URL is on the
            # wrapping div's data-cmp-src attribute instead, present in the
            # static markup regardless of scroll/lazy-load state.
            images = await room.locator("[data-cmp-src]").evaluate_all(
                "els => els.map(e => e.getAttribute('data-cmp-src')).filter(s => s && s.includes('static-dm.barcelo.com'))"
            )
            images = list(dict.fromkeys(images))

            # A room that's genuinely sold out for these dates swaps in a
            # ".c-hotel-room-list__footer-unavailable" block (its sibling,
            # the real price block, still exists in the DOM at that point
            # but holds a stale/placeholder value like "0" or "10.57" rather
            # than a real price — confirmed live: every room with a
            # near-zero price this way is one of the most expensive suite
            # tiers, exactly the ones most likely to sell out first) — this
            # class not having "hidden" is the actual signal to treat the
            # room as unavailable, not the price value itself.
            unavailable_el = room.locator(".c-hotel-room-list__footer-unavailable")
            is_unavailable = False
            if await unavailable_el.count():
                cls = await unavailable_el.first.get_attribute("class") or ""
                is_unavailable = "hidden" not in cls.split()

            price_per_night = None
            if not is_unavailable:
                # data-market-price is inconsistently present (confirmed
                # live: some rooms only carry data-initial-base-value) — the
                # element's own text content ("190") is always there
                # regardless, so read that directly instead of depending on
                # a specific data-* attr.
                price_texts = await room.locator(".c-price__value").evaluate_all(
                    "els => els.map(e => e.textContent || '')"
                )
                for t in price_texts:
                    parsed = _parse_price(t.strip())
                    if parsed is not None:
                        price_per_night = parsed
                        break
                # Belt-and-suspenders: confirmed live the "unavailable"
                # class check above doesn't catch every case (one hotel's
                # priciest suite still showed a stale $10.57/night with that
                # class absent) — no real room at any of these resorts goes
                # for under $30/night, so treat anything below that as the
                # same bogus-placeholder-value situation rather than a real
                # price.
                if price_per_night is not None and price_per_night < 30:
                    price_per_night = None
            price = round(price_per_night * 7, 2) if price_per_night is not None else None

            rooms.append({
                "name": room_name,
                "size_sq_m": size_sq_m,
                "amenities": amenities,
                "images": images,
                "rate_options": [{
                    "rate_id": "",
                    "price": price,
                    "original_price": None,
                    "price_per_night": price_per_night,
                    "pills": [],
                    "currency": "USD",
                }] if price is not None else [],
            })
        except Exception as e:
            print(f"    failed to read room {i}: {e}")

    return rooms


async def search_barcelo_hotel(page, hotel_title):
    """Runs one Barcelo search (Oct 31 - Nov 7) for a specific hotel by its
    exact directory name and returns {"name", "listing_price", "rooms"}."""
    await page.goto(URL, wait_until="domcontentloaded")
    # Client-side session state (localStorage) persists across goto() calls
    # within the same browser context and broke every destination-panel
    # search after the first (same root cause diagnosed and fixed in
    # palladium_script.py/iberostar_script.py: a "recent searches"/
    # fastbooking state object silently carries over) — wipe it and reload
    # so each hotel's destination search starts genuinely clean.
    #
    # Cookies are deliberately left alone, unlike those other two scripts'
    # identical-looking fix — confirmed live that ALSO clearing cookies here
    # breaks something the per-room price AJAX calls depend on: with cookies
    # cleared, the room-list articles stay permanently hidden (price fetch
    # never resolves) for 8/9 hotels; leaving cookies alone and clearing
    # only localStorage avoids that while still fixing the destination-panel
    # carryover.
    try:
        await page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }")
    except Exception:
        pass
    await page.reload(wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    try:
        await page.locator("#didomi-notice-agree-button").click(timeout=3000)
        await page.wait_for_timeout(500)
    except Exception:
        pass

    dest_input = page.locator("#destination-fb")
    # Confirmed live: on the 2nd+ hotel, the input's own decorative
    # placeholder <p> intercepts a plain center-of-element click — its
    # measured bounding box is a zero-height sliver right at the input's
    # own top edge (a transient CSS-transition artifact, not a real modal),
    # so Playwright's default click point lands exactly on it. Clicking a
    # specific point near the bottom of the input instead — still a real,
    # non-forced click, so the framework's own click handler that populates
    # the popover still fires correctly (force=True was tried instead and
    # broke that: it bypassed the interception check but the popover then
    # opened empty, same class of bug as palladium_script.py's dates-field
    # force=True regression) — reliably avoids the sliver. Confirmed live
    # across 3 consecutive searches with zero interception failures.
    await dest_input.click(timeout=10000, position={"x": 20, "y": 40})
    await page.wait_for_timeout(1000)
    item = page.locator("#destination-popover .js-title-hotel", has_text=hotel_title).first
    try:
        await item.scroll_into_view_if_needed()
        await item.click(timeout=10000)
    except Exception as e:
        print(f"WARNING: could not select Barcelo hotel {hotel_title!r}: {type(e).__name__}: {e}")
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe_name = re.sub(r"\W+", "_", hotel_title)
        await page.screenshot(path=os.path.join(DEBUG_DIR, f"{safe_name}_dest_fail.png"), full_page=True)
        return None
    await human_pause()

    try:
        await page.locator("#checkin-fb").click(timeout=10000)
        await page.wait_for_timeout(1500)
        next_btn = page.locator('[aria-label="Next month"]').last
        await click_month_day(page, next_btn, CHECK_IN_MONTH_YEAR, CHECK_IN_DAY)
        await human_pause()
        await click_month_day(page, next_btn, CHECK_OUT_MONTH_YEAR, CHECK_OUT_DAY)
        await human_pause()
    except Exception as e:
        print(f"WARNING: date selection failed for {hotel_title!r}: {type(e).__name__}: {e}")
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe_name = re.sub(r"\W+", "_", hotel_title)
        await page.screenshot(path=os.path.join(DEBUG_DIR, f"{safe_name}_dates_fail.png"), full_page=True)
        return None

    try:
        # The #fastbooking_cta_search_home id matches a hidden duplicate
        # element too (confirmed live — is_visible() on the id selector
        # reports False even though a real Search button is on screen) —
        # a role+visibility query finds the actually-clickable one.
        search_btn = page.get_by_role("button", name="Search").locator("visible=true").first
        await search_btn.click(timeout=10000)
        await page.wait_for_timeout(6000)
    except Exception as e:
        print(f"WARNING: search submit failed for {hotel_title!r}: {type(e).__name__}: {e}")
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe_name = re.sub(r"\W+", "_", hotel_title)
        await page.screenshot(path=os.path.join(DEBUG_DIR, f"{safe_name}_search_fail.png"), full_page=True)
        return None

    try:
        # Confirmed live: the room-list articles exist in the DOM well
        # before they're visible — each room's price is fetched via its own
        # async JSON call (.../roomprice.<hotel>.<room>.json) after initial
        # render, and the container seems to stay hidden until those settle,
        # which occasionally takes longer than a short timeout allows.
        await page.locator(".c-hotel-room-list-JS").first.wait_for(state="visible", timeout=40000)
    except Exception as e:
        print(f"WARNING: no room list appeared for {hotel_title!r}: {type(e).__name__}: {e}")
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe_name = re.sub(r"\W+", "_", hotel_title)
        await page.screenshot(path=os.path.join(DEBUG_DIR, f"{safe_name}_no_rooms.png"), full_page=True)
        return None

    rooms = await parse_barcelo_room_list(page)
    all_prices = [ro["price"] for room in rooms for ro in room["rate_options"] if ro["price"] is not None]
    listing_price = min(all_prices) if all_prices else None
    return {"name": hotel_title, "listing_price": listing_price, "rooms": rooms, "booking_url": page.url}


async def scope_barcelo():
    by_destination = load_barcelo_hotels_by_destination()
    print(f"Barcelo hotels found in scraped jsonl data: {by_destination}")

    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = start_atomic_write(OUTPUT_PATH)
    scraped_at = datetime.date.today().isoformat()
    written = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        directory_names = await fetch_directory(page)
        resolved = resolve_barcelo_hotels(by_destination, directory_names)
        print(f"Resolved {len(resolved)} hotels")
        await page.close()

        for sw_dest, sw_name, hotel_title in resolved:
            # A fresh context per hotel — not just localStorage-cleared and
            # reloaded within the same one — confirmed live to matter here:
            # some accumulating client-side state a few searches into a
            # shared session left the room-list stuck permanently hidden
            # (price-fetch AJAX apparently never resolving) for most hotels,
            # in a way neither clearing localStorage nor clearing cookies
            # alone fully fixed. A genuinely clean context sidesteps needing
            # to know exactly which piece of state was responsible.
            context = await browser.new_context()
            page = await context.new_page()
            try:
                hotel = await search_barcelo_hotel(page, hotel_title)
            except Exception as e:
                print(f"!!! FAILED hotel={hotel_title!r}: {type(e).__name__}: {e}")
                await context.close()
                continue
            await context.close()
            if hotel is None:
                continue

            matched = match_southwest_name(hotel["name"], [sw_name])
            if matched is None:
                print(f"  no Southwest match for Barcelo listing {hotel['name']!r} (expected {sw_name!r}) — skipping")
                continue

            print(f"[{sw_dest}] {hotel['name']!r} — {len(hotel['rooms'])} room(s), listing_price={hotel['listing_price']}")
            record = {
                "chain": "barcelo",
                "destination": sw_dest,
                "southwest_hotel_name": matched,
                "barcelo_name": hotel["name"],
                "departure_date": DEPARTURE_DATE,
                "return_date": RETURN_DATE,
                "scraped_at": scraped_at,
                "listing_price": hotel["listing_price"],
                "currency": "USD",
                "booking_url": hotel["booking_url"],
                "rooms": hotel["rooms"],
            }
            with open(tmp_path, "a") as f:
                f.write(json.dumps(record) + "\n")
            written += 1

        await browser.close()

    if written == 0:
        os.remove(tmp_path)
        raise RuntimeError(f"Wrote 0 hotels — leaving {OUTPUT_PATH} untouched. Check the warnings above.")

    finish_atomic_write(tmp_path, OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH} ({written} hotels)")


if __name__ == "__main__":
    install_process_guards()
    asyncio.run(scope_barcelo())
