import asyncio
import datetime
import difflib
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

URL = "https://www.palladiumhotelgroup.com/en"
# Confirmed live: each result card's own "Book this hotel" link already
# follows this exact pattern (e.g. .../bookcore/availability/gppalace/2026-10-31/2026-11-07/?adults=2&cp=&children-ages=)
# — built directly from data-hotel-codigo rather than clicking through, since
# we already have the code from the results-listing parse.
BOOKING_URL_TEMPLATE = "https://bookings.palladiumhotelgroup.com/bookcore/availability/{code}/{check_in}/{check_out}/?adults=2&cp=&children-ages="
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "data")

DEPARTURE_DATE = "10/31"
RETURN_DATE = "11/07"
CHECK_IN_DATE_ATTR = "2026-10-31"
CHECK_OUT_DATE_ATTR = "2026-11-07"
OUTPUT_PATH = os.path.join(DATA_DIR, "rates_palladium_10-31.jsonl")
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "palladium_debug_out")

# Room size on the room-selection page is reported in square feet
# ("≈515 sq ft"), not square meters — converted for consistency with the
# shared schema's size_sq_m field (same conversion iberostar_script.py uses).
SIZE_SQFT_RE = re.compile(r"(\d+)\s*sq\s*ft", re.IGNORECASE)
SQFT_TO_SQM = 0.092903

# Southwest decorates Palladium/TRS names with suffixes ("All Inc", "All
# Incl.", "- Adults Only", "AI") that Palladium's own directory doesn't
# have, and sometimes truncates names mid-word (confirmed live: "All
# Inclusi", missing "Resort & Spa" entirely on one listing) — handled by
# match_southwest_name's either-direction subset check below, not by trying
# to strip every possible truncation here.
DECORATION_RE = re.compile(
    r"\s*[-–]?\s*(all inclusive|all incl\.?|all inc\.?|adults only|select|ai)\b",
    re.IGNORECASE,
)


def normalize_hotel_name(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = DECORATION_RE.sub("", name)
    name = re.sub(r"[.,]", "", name)  # stray punctuation (e.g. a leftover "Incl." period) shouldn't break token equality
    return re.sub(r"\s+", " ", name).strip().lower()


def match_southwest_name(candidate_name, southwest_names, cutoff=0.75):
    """candidate_name is what Palladium's own site calls a hotel; southwest_names
    are Southwest's decorated strings for the same destination. Returns the
    matching Southwest name (the join key) or None.

    Two things confirmed live make a plain ratio-cutoff check unsafe here:
    1. Southwest's own scraped names are sometimes truncated mid-word
       ("...Costa Mujeres - All Inclusi"), so a fixed direction ("real name
       must contain every Southwest token" or vice versa) breaks depending
       on which side got cut off.
    2. A genuinely wrong hotel can still score dangerously high on pure
       character ratio: "Grand Palladium Select White Sand Resort & Spa"
       vs the real (and unrelated) "Grand Palladium White Island Resort &
       Spa" scored 0.975 — higher than any reasonable cutoff — because both
       are near-identical templates differing by one substituted word.

    Fix: accept a candidate only if, after tokenizing, one side's token set
    is a clean subset of the other's (handles truncation in either
    direction) — a same-length word substitution like sand/island fails
    this in both directions since each side then has a token the other
    lacks, which is exactly what should be rejected.

    3. That subset check alone still isn't enough: confirmed live, "Grand
       Palladium Kantenah Resort & Spa" matched BOTH the real hotel and the
       unrelated "Family Selection at Grand Palladium Kantenah Resort &
       Spa" — every token of the short name is present in the long one, so
       the subset check accepts it, but "Family Selection at" is a genuine
       sub-brand qualifier (same pattern as "The Signature Level..."
       elsewhere), not truncation. The real truncation Southwest exhibits
       is always trailing (names get cut off at a length limit, never at
       the start) — so also require the first word of both sides to match,
       which trailing-truncation and word-order/connector differences
       (and/&, missing "at") satisfy for free, but a prefixed qualifier
       never does."""
    normalized_to_original = {normalize_hotel_name(n): n for n in southwest_names}
    target = normalize_hotel_name(candidate_name)
    target_words = target.split()
    target_tokens = {t for t in target_words if len(t) > 2}
    matches = difflib.get_close_matches(target, normalized_to_original.keys(), n=3, cutoff=cutoff)
    for m in matches:
        candidate_words = m.split()
        if not candidate_words or not target_words or candidate_words[0] != target_words[0]:
            continue
        candidate_tokens = {t for t in candidate_words if len(t) > 2}
        if not (target_tokens - candidate_tokens) or not (candidate_tokens - target_tokens):
            return normalized_to_original[m]
    return None


def load_palladium_hotels_by_destination():
    """Read app/data/hotels_*.jsonl directly — not via app.services.southwest
    — so this scraper stays independent of the FastAPI app. Groups
    Palladium/TRS-branded entries (Grand Palladium, TRS, and "The Signature
    Level" sub-brand) by Southwest's destination tag, since that's the join
    key the FastAPI app's rates service keys off of."""
    by_destination = {}
    for path in glob.glob(os.path.join(DATA_DIR, "hotels_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                hotel = json.loads(line)
                if re.search(r"\btrs\b|\bpalladium\b", hotel["name"], re.IGNORECASE):
                    by_destination.setdefault(hotel["destination"], []).append(hotel["name"])
    return by_destination


async def fetch_directory(page):
    """Palladium's homepage embeds its entire worldwide hotel+destination
    directory directly in the initial HTML (confirmed live: 44 hotel
    entries and 22 destination entries present even with zero typing in
    the search box — each hidden via CSS until the box's text matches, not
    added to the DOM reactively). Parsing it directly here is far more
    reliable than reverse-engineering the destination picker's live
    filtering, and is what lets us resolve precisely which of Palladium's
    own destinations (much more granular than Southwest's — e.g. Southwest
    tags 6 hotels "Cancun", but Palladium itself splits them across
    "Cancun: Costa Mujeres" and "Riviera Maya") each target hotel actually
    lives under, entirely from data instead of trial-and-error searching."""
    await page.goto(URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    try:
        await page.locator("#onetrust-accept-btn-handler").click(force=True, timeout=3000)
        await page.wait_for_timeout(500)
    except Exception:
        pass

    html = await page.content()

    hotels = []
    for m in re.finditer(r'<li[^>]*data-provider-id="([^"]*)"[^>]*data-ciudad="([^"]*)"[^>]*>([^<]*)</li>', html):
        provider_id, destination_id, name = m.groups()
        name = name.strip().replace("&amp;", "&")
        if name:
            hotels.append({"provider_id": provider_id, "destination_id": destination_id, "name": name})

    dest_by_id = {}
    for m in re.finditer(r'<li[^>]*data-destination-id="([^"]*)"[^>]*data-is-hotel="false"[^>]*><span>([^<]*)</span>', html):
        dest_by_id[m.group(1)] = m.group(2).strip()

    return dest_by_id, hotels


def resolve_palladium_destinations(palladium_by_destination, dest_by_id, hotel_directory):
    """Maps each Southwest-tagged Palladium/TRS hotel to the Palladium
    destination title to actually search for it. Returns
    {palladium_destination_title: [(southwest_destination, hotel_name), ...]}.
    Reuses match_southwest_name (with its either-direction token-check, just
    with a looser cutoff) rather than a raw difflib call — a raw call here
    is exactly what let "Grand Palladium Select White Sand Resort & Spa"
    silently group under Ibiza's unrelated "Grand Palladium White Island
    Resort & Spa" (ratio 0.975) before match_southwest_name existed to catch
    it; the authoritative match still happens later against the real
    returned hotel names, this only decides which destination to search."""
    hotel_by_name = {h["name"]: h for h in hotel_directory}

    by_palladium_dest = {}
    for southwest_destination, hotel_names in palladium_by_destination.items():
        for name in hotel_names:
            matched_directory_name = match_southwest_name(name, list(hotel_by_name.keys()), cutoff=0.6)
            if matched_directory_name is None:
                print(f"WARNING: {name!r} not found in Palladium's own hotel directory — skipping")
                continue
            h = hotel_by_name[matched_directory_name]
            dest_title = dest_by_id.get(h["destination_id"])
            if dest_title is None:
                print(f"WARNING: {name!r} matched directory hotel {h['name']!r} but its destination id {h['destination_id']!r} isn't in the directory — skipping")
                continue
            by_palladium_dest.setdefault(dest_title, []).append((southwest_destination, name))
    return by_palladium_dest


def _parse_palladium_price(text):
    """'US$ 2,421.65' or 'US$ 345.95 / night' -> 2421.65 / 345.95."""
    if not text:
        return None
    m = re.search(r"[\d,]+\.?\d*", text)
    if not m:
        return None
    return float(m.group().replace(",", ""))


async def parse_palladium_room_page(page):
    """The room-selection page (bookcore/availability/<code>/...) is a
    completely different React micro-frontend from the jQuery-based
    results-listing widget (confirmed live) — parsed via its data-testid
    hooks rather than styled-components CSS classes, since testids are the
    stable contract here (the class names carry a build-specific hash,
    e.g. "...-sc-11o9vyw-8", that isn't meant to be relied on)."""
    rooms = []
    room_els = page.locator('[data-testid="fn-desktop-availability-page-room"]')
    room_count = await room_els.count()
    for i in range(room_count):
        room = room_els.nth(i)
        try:
            name_el = room.locator('h3[class*="DesktopAvailabilityItemstyles__TitleStyles"]')
            room_name = (await name_el.first.inner_text()).strip() if await name_el.count() else ""

            desc_el = room.locator('[data-testid="fn-read-more"] p')
            desc_text = (await desc_el.first.inner_text()) if await desc_el.count() else ""
            size_match = SIZE_SQFT_RE.search(desc_text)
            size_sq_m = round(int(size_match.group(1)) * SQFT_TO_SQM) if size_match else None

            # all_inner_texts() (Playwright's .innerText) returns "" here —
            # confirmed live: the visible tooltip span has zero layout size
            # until hovered, and .innerText is visibility-aware, unlike
            # .textContent. Scoping directly to the accessibility-only
            # "ServicesHidden" span (always has real textContent) and
            # reading that via evaluate_all sidesteps both problems: no
            # empty results, and no duplicate text from the tooltip span.
            amenities_raw = await room.locator('[data-testid="fn-services-list"] li [class*="ServicesHidden"]').evaluate_all(
                "els => els.map(e => e.textContent || '')"
            )
            amenities = [a.strip() for a in amenities_raw if a.strip()]

            images = await room.locator('[data-testid="fn-cdn-img"]').evaluate_all(
                "els => els.map(e => e.getAttribute('src')).filter(Boolean)"
            )
            images = list(dict.fromkeys(images))

            rate_options = []
            boards = room.locator('[data-testid="fn-board"]')
            for j in range(await boards.count()):
                board = boards.nth(j)

                board_name_el = board.locator('[class*="TooltipNameStyles"]')
                board_name = (await board_name_el.first.inner_text()).strip() if await board_name_el.count() else ""

                price = None
                total_el = board.locator('[data-testid="fn-board-total-price"]')
                if await total_el.count():
                    price = _parse_palladium_price(await total_el.first.get_attribute("data-price"))

                original_price = None
                discount_el = board.locator('[data-testid="fn-board-discount-price"]')
                if await discount_el.count():
                    original_price = _parse_palladium_price(await discount_el.first.inner_text())

                price_per_night = None
                avg_el = board.locator('[data-testid="fn-board-average-price"]')
                if await avg_el.count():
                    price_per_night = _parse_palladium_price(await avg_el.first.inner_text())

                rate_options.append({
                    "rate_id": "",
                    "price": price,
                    "original_price": original_price,
                    "price_per_night": price_per_night,
                    "pills": [board_name] if board_name else [],
                    "currency": "USD",
                })

            rooms.append({
                "name": room_name,
                "size_sq_m": size_sq_m,
                "amenities": amenities,
                "images": images,
                "rate_options": rate_options,
            })
        except Exception as e:
            print(f"    failed to read room {i} on Palladium room page: {e}")

    return rooms


async def search_palladium_destination(page, destination_title):
    """Run one Palladium destination search (Oct 31 - Nov 7) and return a
    list of {"name", "code", "listing_price", "original_price"} dicts, one
    per hotel on the (server-rendered, no async job polling needed —
    confirmed live) results page."""
    await page.goto(URL, wait_until="domcontentloaded")
    # The site keeps a client-side "recent searches" list (confirmed live —
    # it showed up in the destination panel after the first search) —
    # cookies/localStorage are scoped to the browser context, not the page,
    # so that state persists across goto() calls and broke every
    # destination after the first (same root cause diagnosed in
    # iberostar_script.py: calendar navigation silently stopped working on
    # the 2nd+ destination). Wipe both and reload so each search starts clean.
    try:
        await page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }")
    except Exception:
        pass
    await page.context.clear_cookies()
    await page.reload(wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    try:
        await page.locator("#onetrust-accept-btn-handler").click(force=True, timeout=3000)
        await page.wait_for_timeout(500)
    except Exception:
        pass

    # The real #destinoHotel input starts display:none — a decorative
    # "#hotel-destino-text" span is what's actually visible and must be
    # clicked first to reveal it (confirmed live).
    await page.locator("#hotel-destino-text").click()
    await human_pause()
    await page.locator("#destinoHotel").fill(destination_title)
    await human_pause()

    dest_li = page.locator('li[data-is-hotel="false"]', has_text=destination_title)
    try:
        await dest_li.first.wait_for(state="visible", timeout=10000)
        await dest_li.first.click(force=True)
    except Exception as e:
        print(f"WARNING: could not select Palladium destination {destination_title!r}: {type(e).__name__}: {e}")
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe_name = re.sub(r"\W+", "_", destination_title)
        await page.screenshot(path=os.path.join(DEBUG_DIR, f"{safe_name}_dest_fail.png"), full_page=True)
        return []
    await human_pause()

    try:
        # NOT force=True here (unlike the destination-panel click above) —
        # confirmed live this is the actual bug: force=True dispatches a raw
        # click event that Vue's handler doesn't reliably pick up, so the
        # calendar silently never opens on the 2nd+ destination in a run. A
        # real click (with Playwright's normal actionability wait) works.
        await page.locator(".fb-field.dates").click()
        await page.locator("button.drp-next").first.wait_for(state="visible", timeout=10000)
        # A fixed click-count here (previously range(2)) silently drifts
        # wrong as real-world time passes rather than erroring — the
        # daterangepicker only renders <td data-date> cells for whichever
        # months are currently displayed, so clicking "next" the wrong
        # number of times just means the target cell never exists (same
        # failure class confirmed live in riu_script.py's own calendar).
        # Clicking until the target date-cell is actually present, capped
        # generously, is correct regardless of which month "today" is.
        check_in_cell = page.locator(f'td[data-date^="{CHECK_IN_DATE_ATTR}"]')
        for _ in range(8):
            if await check_in_cell.count():
                break
            await page.locator("button.drp-next").click()
            await page.wait_for_timeout(300)
        else:
            raise RuntimeError(f"{CHECK_IN_DATE_ATTR} never appeared in the calendar after 8 next-month clicks")
        await check_in_cell.click()
        await human_pause()
        await page.locator(f'td[data-date^="{CHECK_OUT_DATE_ATTR}"]').click()
        await human_pause()
    except Exception as e:
        print(f"WARNING: date selection failed for {destination_title!r}: {type(e).__name__}: {e}")
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe_name = re.sub(r"\W+", "_", destination_title)
        await page.screenshot(path=os.path.join(DEBUG_DIR, f"{safe_name}_dates_fail.png"), full_page=True)
        return []

    try:
        # force=True here is safe (unlike the dates-field click earlier,
        # where force=True was the actual bug) — this is a genuine visual
        # overlap, not a "needs a real interaction to fire" case: a
        # promotional header bar intercepts the click after visiting a
        # hotel's own booking-subdomain room page (confirmed live, only on
        # destinations processed after the first one does that round trip).
        # Force still dispatches the event on the Search button itself, not
        # whatever's visually on top of it.
        #
        # wait_until="domcontentloaded", not the default "load" — the
        # results page pulls in dozens of slow third-party analytics
        # scripts (Adobe Target, Zeta, Boomtrain, Sojern, ...), so waiting
        # for the full "load" event routinely exceeded 20s later in a run
        # (confirmed live) even though the page we actually need was ready
        # much sooner.
        async with page.expect_navigation(timeout=30000, wait_until="domcontentloaded"):
            await page.get_by_role("button", name="Search", exact=True).click(force=True)
    except Exception as e:
        print(f"WARNING: search submission failed for {destination_title!r}: {type(e).__name__}: {e}")
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe_name = re.sub(r"\W+", "_", destination_title)
        await page.screenshot(path=os.path.join(DEBUG_DIR, f"{safe_name}_search_fail.png"), full_page=True)
        return []

    await page.wait_for_timeout(3000)

    cards = page.locator(".roi-hotel.js-hotel-availability-result")
    count = await cards.count()
    print(f"[{destination_title}] found {count} hotels")

    results = []
    for i in range(count):
        card = cards.nth(i)
        try:
            code = await card.get_attribute("data-hotel-codigo")
            # Each card carries two identical "Book this hotel" links
            # (confirmed live: same aria-label/href on both — a responsive
            # desktop/mobile markup duplicate, not an ambiguous choice).
            book_link = card.locator("a.roi-hotel__button").first
            aria_label = await book_link.get_attribute("aria-label") or ""
            name = re.sub(r"^Book this hotel\s*", "", aria_label).strip()

            # Same responsive-duplicate pattern as the book link — guard
            # with .first here too rather than waiting to hit the same
            # strict-mode violation on the next run.
            price_el = card.locator(".roi-hotel__price-total-value").first
            listing_price = float(await price_el.get_attribute("data-price")) if await price_el.count() else None

            original_price = None
            old_price_el = card.locator(".roi-hotel__price-old-value").first
            if await old_price_el.count():
                original_price = float(await old_price_el.get_attribute("data-price"))

            results.append({
                "name": name,
                "code": code,
                "listing_price": listing_price,
                "original_price": original_price,
                "booking_url": None,
                "rooms": [],
            })
        except Exception as e:
            print(f"    failed to read hotel card {i} for {destination_title!r}: {e}")

    # Room-level detail lives on each hotel's own booking page, not the
    # results listing — visited after finishing the listing-page loop above
    # (navigating away mid-loop would invalidate the `cards` locator).
    for hotel in results:
        if not hotel["code"]:
            continue
        room_url = BOOKING_URL_TEMPLATE.format(code=hotel["code"], check_in=CHECK_IN_DATE_ATTR, check_out=CHECK_OUT_DATE_ATTR)
        hotel["booking_url"] = room_url
        try:
            await page.goto(room_url, wait_until="domcontentloaded")
            await page.locator('[data-testid="fn-desktop-availability-page-room"]').first.wait_for(state="visible", timeout=20000)
            await page.wait_for_timeout(1000)
            hotel["rooms"] = await parse_palladium_room_page(page)
            print(f"    {hotel['name']!r}: {len(hotel['rooms'])} room(s)")
        except Exception as e:
            print(f"    WARNING: failed to load room detail for {hotel['name']!r} ({hotel['code']}): {type(e).__name__}: {e}")
            os.makedirs(DEBUG_DIR, exist_ok=True)
            safe_name = re.sub(r"\W+", "_", hotel["code"])
            await page.screenshot(path=os.path.join(DEBUG_DIR, f"{safe_name}_room_page_fail.png"), full_page=True)

    return results


async def scope_palladium():
    palladium_by_destination = load_palladium_hotels_by_destination()
    print(f"Palladium/TRS hotels found in scraped jsonl data: {palladium_by_destination}")

    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = start_atomic_write(OUTPUT_PATH)
    scraped_at = datetime.date.today().isoformat()
    written = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        dest_by_id, hotel_directory = await fetch_directory(page)
        by_palladium_dest = resolve_palladium_destinations(palladium_by_destination, dest_by_id, hotel_directory)
        print(f"Resolved to {len(by_palladium_dest)} Palladium-side destinations: {sorted(by_palladium_dest.keys())}")

        for palladium_dest_title, expected in by_palladium_dest.items():
            try:
                hotels = await search_palladium_destination(page, palladium_dest_title)
            except Exception as e:
                print(f"!!! FAILED destination={palladium_dest_title!r}: {type(e).__name__}: {e}")
                continue

            expected_by_sw_dest = {}
            for sw_dest, name in expected:
                expected_by_sw_dest.setdefault(sw_dest, []).append(name)

            for hotel in hotels:
                if not hotel["name"]:
                    continue

                matched = None
                matched_sw_dest = None
                for sw_dest, candidate_names in expected_by_sw_dest.items():
                    m = match_southwest_name(hotel["name"], candidate_names)
                    if m is not None:
                        matched, matched_sw_dest = m, sw_dest
                        break
                if matched is None:
                    print(f"  no Southwest match for Palladium listing {hotel['name']!r} — skipping")
                    continue

                record = {
                    "chain": "palladium",
                    "destination": matched_sw_dest,
                    "southwest_hotel_name": matched,
                    "palladium_name": hotel["name"],
                    "departure_date": DEPARTURE_DATE,
                    "return_date": RETURN_DATE,
                    "scraped_at": scraped_at,
                    "listing_price": hotel["listing_price"],
                    "original_price": hotel["original_price"],
                    "currency": "USD",
                    "booking_url": hotel.get("booking_url"),
                    "rooms": hotel["rooms"],
                }
                with open(tmp_path, "a") as f:
                    f.write(json.dumps(record) + "\n")
                written += 1

        await browser.close()

    if written == 0:
        # See riu_script.py's identical guard — a 0-record run is a broken
        # selector/site change, not "no hotels," and shouldn't be allowed to
        # clobber a previous good file via finish_atomic_write's
        # unconditional swap.
        os.remove(tmp_path)
        raise RuntimeError(f"Wrote 0 hotels — leaving {OUTPUT_PATH} untouched. Check the warnings above.")

    finish_atomic_write(tmp_path, OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH} ({written} hotels)")


if __name__ == "__main__":
    install_process_guards()
    asyncio.run(scope_palladium())
