import asyncio
import datetime
import difflib
import glob
import json
import os
import re
from playwright.async_api import async_playwright

from scraping_common import (
    finish_atomic_write,
    human_pause,
    install_process_guards,
    start_atomic_write,
)

URL = "https://www.riu.com/en"
BASE_URL = "https://www.riu.com"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "data")

DEPARTURE_DATE = "10/31"
RETURN_DATE = "11/07"
OUTPUT_PATH = os.path.join(DATA_DIR, "rates_riu_10-31.jsonl")

# Suffixes Southwest decorates hotel names with that RIU's own listing names
# won't have (e.g. Southwest: "Riu Santa Fe - All Inclusive" vs RIU's own
# "Hotel Riu Santa Fe 5 *") — stripped before comparing the two.
DECORATION_RE = re.compile(
    r"\s*-\s*(adults only|all inclusive|ai)\b|\bhotel\b|\d\s*\*",
    re.IGNORECASE,
)

# Listing cards show "7 night(s) from\n\nfrom\n1,583 USD" — this is the
# "from" line specifically, not the crossed-out "Normal price" line.
LISTING_PRICE_RE = re.compile(r"\bfrom\n([\d,]+)\s*USD")

# Confirmed live: "Balcony or terrace | 25 sq m"
SIZE_RE = re.compile(r"(\d+)\s*sq\s*m", re.IGNORECASE)

# Confirmed live, scoped to a single <riu-prices-card>'s own text, so there's
# no ambiguity between tiers the way a whole-room-card regex would have.
PER_NIGHT_RE = re.compile(r"price per night:\s*([\d,]+)\s*USD")

# RIU's own destination picker doesn't always use Southwest's label for a
# region — e.g. Southwest groups its one Nassau hotel as "Nassau", but RIU's
# picker only offers "Bahamas" / "Paradise Island" (confirmed live: typing
# "Nassau" returns zero destination options). "Aruba" is a different flavor
# of the same problem: the picker nests a nested leaf ("Aruba-Palm Beach")
# under the "Aruba" category, and both RIU hotels live under that leaf, not
# the category — clicking the "Aruba (" category button selects a node with
# no hotels of its own, which is why it silently returned 0 results instead
# of warning (confirmed live via screenshot: only "Aruba-Palm Beach (2)" is
# checked/selectable, "Aruba (2)" is just the parent grouping). Maps the
# Southwest destination name to the term to actually type into RIU's search
# box.
RIU_DESTINATION_QUERY_OVERRIDES = {
    "Nassau": "Paradise Island",
    "Aruba": "Aruba-Palm Beach",
}


def _parse_price(text):
    return float(text.strip().replace(",", "").replace("USD", "").strip())


def parse_listing_price(card_text):
    match = LISTING_PRICE_RE.search(card_text)
    return float(match.group(1).replace(",", "")) if match else None


def load_riu_hotels_by_destination():
    """Read app/data/hotels_*.jsonl directly — not via app.services.southwest
    — so this scraper stays independent of the FastAPI app (it only needs to
    agree on the on-disk file shape). Groups RIU-branded entries by
    destination, since that's what decides which RIU searches to run."""
    by_destination = {}
    for path in glob.glob(os.path.join(DATA_DIR, "hotels_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                hotel = json.loads(line)
                # Word-boundary match — a plain substring check also matches
                # "Atrium" (which contains "riu" as letters), pulling in
                # unrelated hotels.
                if re.search(r"\briu\b", hotel["name"], re.IGNORECASE):
                    by_destination.setdefault(hotel["destination"], []).append(hotel["name"])
    return by_destination


def normalize_hotel_name(name):
    name = DECORATION_RE.sub("", name)
    return re.sub(r"\s+", " ", name).strip().lower()


def match_southwest_name(riu_card_name, candidate_names):
    """riu_card_name is what's shown on RIU's own results page (e.g. "Hotel
    Riu Santa Fe 5 *"); candidate_names are Southwest's decorated strings for
    the same destination (e.g. "Riu Santa Fe - All Inclusive"). Returns the
    matching Southwest name (the join key) or None.

    cutoff=0.9, not the old 0.5: a genuine match normalizes to the exact same
    string on both sides (ratio 1.0), since normalize_hotel_name() strips all
    the decoration that differs between RIU's and Southwest's naming. A
    low cutoff instead let RIU hotels Southwest doesn't even sell (e.g. "Riu
    Tequila", "Riu Playacar" in Cancun) get force-matched to an unrelated
    Southwest listing on nothing but shared "riu " prefix + similar length —
    confirmed live, those scored 0.57-0.75 against the closest real
    candidate, well clear of any genuine match. get_close_matches returning
    nothing (None) for those is correct: they simply aren't in Southwest's
    listings, not a fuzzy-match target on their own."""
    normalized_to_original = {normalize_hotel_name(n): n for n in candidate_names}
    target = normalize_hotel_name(riu_card_name)
    matches = difflib.get_close_matches(target, normalized_to_original.keys(), n=1, cutoff=0.9)
    return normalized_to_original[matches[0]] if matches else None


async def get_room_images(room_card, page):
    """Room cards only show one static thumbnail by default — the full set
    lives behind the "Gallery" button ("Ver galeria" in its aria-label — the
    site's i18n missed this one) in an overlay dialog that Angular portals
    to the end of <body>, not nested under the room card, so it has to be
    queried at the page level once opened, then closed again before moving
    on to the next room. Scoped to .riu-ui-dialog-item specifically because
    a hidden OneTrust cookie-preferences dialog also matches [role=dialog]
    and would otherwise leak unrelated logo images into the result."""
    gallery_btn = room_card.locator('button[aria-label="Ver galeria"]')
    if await gallery_btn.count() == 0:
        return []

    await human_pause()
    await gallery_btn.first.click()
    dialog = page.locator(".riu-ui-dialog-item").first
    await dialog.wait_for(state="visible", timeout=10000)

    srcs = await dialog.locator("img").evaluate_all(
        "els => els.map(e => e.getAttribute('src') || e.getAttribute('data-src')).filter(Boolean)"
    )
    images = [s if s.startswith("http") else f"{BASE_URL}{s}" for s in srcs]

    await human_pause()
    await dialog.get_by_role("button", name="Close").click()
    await dialog.wait_for(state="hidden", timeout=10000)

    return images


async def get_room_data(room_card, page):
    """room_card is a Locator for a single <riu-room-card>."""
    name = await room_card.locator(".riu-room-card__header p").first.inner_text()

    size_text = await room_card.locator(".riu-room-card__header p.o-type-md").first.inner_text()
    size_match = SIZE_RE.search(size_text)
    size_sq_m = int(size_match.group(1)) if size_match else None

    amenities = await room_card.locator(
        ".riu-image-card__services__list-container span.o-type-sm"
    ).all_inner_texts()

    images = await get_room_images(room_card, page)

    rate_options = []
    rate_cards = room_card.locator("riu-prices-card")
    for j in range(await rate_cards.count()):
        rate_card = rate_cards.nth(j)
        rate_id = await rate_card.get_attribute("id") or ""

        price_text = await rate_card.locator("[data-ux='price_tarifa']").first.inner_text()
        price = _parse_price(price_text)

        original_price = None
        discount_locator = rate_card.locator(".riu-price-card__discount")
        if await discount_locator.count():
            original_price = _parse_price(await discount_locator.first.inner_text())

        rate_text = await rate_card.inner_text()
        per_night_match = PER_NIGHT_RE.search(rate_text)
        price_per_night = float(per_night_match.group(1).replace(",", "")) if per_night_match else None

        pills = await rate_card.locator(".riu-price-card__pills .c-pill").all_inner_texts()

        rate_options.append({
            "rate_id": rate_id,
            "price": price,
            "original_price": original_price,
            "price_per_night": price_per_night,
            "pills": [p.strip() for p in pills if p.strip()],
            "currency": "USD",
        })

    return {
        "name": name,
        "size_sq_m": size_sq_m,
        "amenities": [a.strip() for a in amenities if a.strip()],
        "images": images,
        "rate_options": rate_options,
    }


async def search_destination(page, destination):
    """Run one RIU destination search (Oct 31 - Nov 7) and return every
    hotel card found, each with its listing price and full room/rate data."""
    await page.goto(URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)

    for selector in ["#onetrust-accept-btn-handler", "#onetrust-pc-btn-handler", ".dy-lb-close"]:
        try:
            await page.locator(selector).click(force=True, timeout=3000)
            await page.wait_for_timeout(500)
        except Exception:
            pass  # already dismissed / didn't appear this time — fine either way

    await page.evaluate(
        """
        () => {
            document.querySelectorAll(
                '#onetrust-consent-sdk, #onetrust-banner-sdk, .onetrust-pc-dark-filter, .onetrust-pc-backdrop, .onetrust-overlay, .dy-modal-container, .dy-modal-backdrop, .dy-modal-wrapper, .dy-modal-contents, .dy-modal-content, .dy-act-overlay'
            ).forEach(el => el.remove());
        }
        """
    )
    await page.wait_for_timeout(500)

    riu_query = RIU_DESTINATION_QUERY_OVERRIDES.get(destination, destination)

    destination_input = page.get_by_placeholder("Select a hotel or destination")
    await destination_input.fill(riu_query)
    await human_pause()

    try:
        await destination_input.click(force=True, timeout=5000)
    except Exception:
        pass
    await human_pause()

    if await page.locator("select-location").count() == 0:
        await human_pause()
        try:
            await destination_input.click(force=True, timeout=5000)
        except Exception:
            pass
        await human_pause()

    try:
        await page.locator("select-location").first.wait_for(state="visible", timeout=15000)

        # Scoped to the "DESTINATIONS" column specifically (data-qa="choose-destiny"),
        # not the whole panel — the sibling "HOTELS IN X" column can contain a
        # hotel whose own name embeds the destination (e.g. "Hotel Riu Palace
        # Aruba", "Hotel Riu Cancun"), which a panel-wide title match picks up
        # too. title^="{destination} (" (starts-with, not contains) further
        # excludes nested sub-regions like "Aruba-Palm Beach (2)" that also
        # contain the destination name as a substring but with a different
        # suffix after it.
        destination_button = page.locator(
            f'li[data-qa="choose-destiny"] button[title^="{riu_query} ("]'
        )

        if await destination_button.count():
            await destination_button.first.scroll_into_view_if_needed()
            await destination_button.first.click(force=True, timeout=15000)
            await human_pause()
        else:
            print(f"WARNING: could not select RIU destination {destination!r} (queried {riu_query!r})")
    except Exception as e:
        print(f"WARNING: destination selection failed for {destination!r}: {type(e).__name__}: {e}")

    # Selecting the destination just fills/highlights it — it doesn't open
    # the calendar on its own. This click on the date-input is what actually
    # reveals it; without it, "next-month" never appears no matter what
    # happened above, which is why every destination failed identically.
    await human_pause()
    await page.locator(".riu-ui-input__element").first.click()

    # A fixed click-count here (previously range(3)) silently drifts wrong
    # as real-world time passes — confirmed live: it correctly reached
    # October 2026 while "today" was in an earlier month, then started
    # overshooting straight to November/December once "today" advanced,
    # with no error, just wrong (or in this case entirely absent) dates.
    # Clicking until the target month is actually visible, capped
    # generously, is correct regardless of which month "today" is.
    oct_month = page.locator("article", has_text="October 2026")
    for _ in range(8):
        if await oct_month.count():
            break
        await human_pause()
        await page.locator("button[data-qa='next-month']").last.click()
        await page.wait_for_timeout(300)
    else:
        raise RuntimeError("October 2026 never appeared in the calendar after 8 next-month clicks")
    # force=True: confirmed live the day cell itself is visible/enabled and
    # the click keeps retrying against a sticky <header> that incidentally
    # overlaps it (not a case where the click needs a genuine
    # interaction-triggered side effect blocked by force — the header is
    # just in the way visually).
    await oct_month.get_by_role("button", name="31", exact=True).click(force=True)
    await human_pause()

    # Confirmed live: selecting the check-in date re-centers the widget
    # around THAT month (showing September/October, not October/November) —
    # it doesn't just keep the previously-visible pair, so November may no
    # longer be on screen at all. Same "click until visible" loop as above,
    # not a fixed count, for the same date-drift-proofing reason.
    nov_month = page.locator("article", has_text="November 2026")
    for _ in range(8):
        if await nov_month.count():
            break
        await human_pause()
        await page.locator("button[data-qa='next-month']").last.click()
        await page.wait_for_timeout(300)
    else:
        raise RuntimeError("November 2026 never appeared in the calendar after 8 next-month clicks")
    await nov_month.get_by_role("button", name="7", exact=True).click(force=True)

    await human_pause()
    await page.get_by_role("button", name="Search", exact=True).click()
    try:
        # count() doesn't auto-wait — it snapshots whatever's in the DOM
        # right now. A flat sleep here raced the Angular render (confirmed
        # live: 0 cards at 4s, full count at 5s for a 15-card destination),
        # so wait for the first card instead of guessing a fixed delay.
        await page.locator("riu-hotel-data").first.wait_for(state="visible", timeout=20000)
        await page.wait_for_timeout(1000)  # let any remaining cards settle in
    except Exception:
        pass  # genuinely no hotels for this destination/date combo

    count = await page.locator("riu-hotel-data").count()
    print(f"[{destination}] found {count} hotels")

    results = []
    for i in range(count):
        # Re-locate fresh each loop — after go_back() the DOM re-renders, so
        # a locator captured before navigating could point at a stale
        # detached node.
        card = page.locator("riu-hotel-data").nth(i)
        name = await card.locator("h2, h3").first.inner_text()
        listing_price = parse_listing_price(await card.inner_text())
        print(f"  [{i}] {name} — listing price: {listing_price}")

        rooms = []
        # Deliberately left None, not page.url — confirmed live that RIU's
        # room-detail route doesn't encode the hotel at all (every one of 25
        # hotels across a full run landed on the identical
        # "https://www.riu.com/en/booking/rooms", and the results card has
        # no <a href> either). A link to that URL would look hotel-specific
        # without being one — worse than no link, since it's actively
        # misleading rather than just absent.
        booking_url = None
        navigated = False
        try:
            await human_pause()
            async with page.expect_navigation(timeout=15000):
                await card.get_by_role("button", name="View rooms").click()
            navigated = True
            await page.wait_for_timeout(3000)

            room_cards = page.locator("riu-room-card")
            room_count = await room_cards.count()
            # The last card's own header can still be rendering even after
            # the count is stable (same class of issue as
            # southwest_script.py's wait_for_result_count) — wait for it
            # specifically instead of trusting room_count alone.
            if room_count:
                await room_cards.nth(room_count - 1).locator(
                    ".riu-room-card__header p"
                ).first.wait_for(state="visible", timeout=15000)

            for r in range(room_count):
                # Isolate per room — one bad/slow card shouldn't cost us the
                # rooms we already read successfully before it.
                try:
                    rooms.append(await get_room_data(room_cards.nth(r), page))
                except Exception as e:
                    print(f"    failed to read room {r} for {name!r}: {e}")
        except Exception as e:
            print(f"    failed to load rooms for {name!r}: {e}")

        results.append({"name": name, "listing_price": listing_price, "booking_url": booking_url, "rooms": rooms})

        if not navigated:
            # "View rooms" never fired (e.g. sold-out card with no button) —
            # we're still on the results list, so there's nothing to go back
            # from. go_back() here would instead unwind to whatever page
            # preceded this search, and the wait_for below would then fail
            # since riu-hotel-data doesn't exist there — previously this
            # raised and lost every hotel already scraped for this
            # destination, not just the one that failed.
            continue

        try:
            # Back to the results list, ready for the next hotel.
            await human_pause()
            await page.go_back()
            await page.locator("riu-hotel-data").first.wait_for(state="visible", timeout=15000)
        except Exception as e:
            print(f"    couldn't return to results list after {name!r}: {e} — stopping this destination early")
            break

    return results


async def scope_riu():
    riu_by_destination = load_riu_hotels_by_destination()
    print(f"RIU hotels found in scraped jsonl data: {riu_by_destination}")

    os.makedirs(DATA_DIR, exist_ok=True)
    # Written to a temp file and swapped in atomically at the end, so a
    # crash mid-scrape leaves the previous good file untouched instead of
    # clobbering it with an empty/partial truncation.
    tmp_path = start_atomic_write(OUTPUT_PATH)
    scraped_at = datetime.date.today().isoformat()
    written = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        for destination, southwest_names in riu_by_destination.items():
            try:
                riu_hotels = await search_destination(page, destination)
            except Exception as e:
                print(f"!!! FAILED destination={destination!r}: {type(e).__name__}: {e}")
                continue

            for hotel in riu_hotels:
                matched_name = match_southwest_name(hotel["name"], southwest_names)
                if matched_name is None:
                    print(f"  no Southwest match for RIU listing {hotel['name']!r} — skipping")
                    continue

                record = {
                    "chain": "riu",
                    "destination": destination,
                    "southwest_hotel_name": matched_name,
                    "riu_name": hotel["name"],
                    "departure_date": DEPARTURE_DATE,
                    "return_date": RETURN_DATE,
                    "scraped_at": scraped_at,
                    "listing_price": hotel["listing_price"],
                    "currency": "USD",
                    "booking_url": hotel.get("booking_url"),
                    "rooms": hotel["rooms"],
                }
                with open(tmp_path, "a") as f:
                    f.write(json.dumps(record) + "\n")
                written += 1

        await browser.close()

    if written == 0:
        # A run that completes with zero records is almost certainly a
        # broken selector/site change, not "RIU genuinely has no hotels
        # anymore" — confirmed live: a calendar-click regression once wiped
        # a good 24-hotel file down to empty because finish_atomic_write
        # unconditionally swaps in the new (empty) file. Leave the previous
        # good output alone and fail loudly instead.
        os.remove(tmp_path)
        raise RuntimeError(f"Wrote 0 hotels — leaving {OUTPUT_PATH} untouched. Check the warnings above.")

    finish_atomic_write(tmp_path, OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH} ({written} hotels)")


if __name__ == "__main__":
    install_process_guards()
    asyncio.run(scope_riu())
