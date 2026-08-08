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

URL = "https://www.bahiaprincipe.com/en/"
# Confirmed live: the homepage's own destination-search panel already lists
# every hotel worldwide as a <button data-hotel-id="..." data-roibackcode="...">,
# present in the DOM without any typing/interaction — no need to search at
# all, just load the page and read the buttons directly (same "parse the
# whole directory from unfiltered markup" shortcut palladium_script.py's
# fetch_directory uses). Bahia Principe runs the same Roiback booking
# platform as Palladium (confirmed live: room-selection page responds to
# palladium_script.py's own parse_palladium_room_page selectors unmodified),
# just on its own subdomain/URL template.
BOOKING_URL_TEMPLATE = "https://en.book.bahia-principe.com/bookcore/availability/{code}/{check_in}/{check_out}/?adults=2&cp=&children-ages="
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "data")

DEPARTURE_DATE = "10/31"
RETURN_DATE = "11/07"
CHECK_IN_DATE_ATTR = "2026-10-31"
CHECK_OUT_DATE_ATTR = "2026-11-07"
OUTPUT_PATH = os.path.join(DATA_DIR, "rates_bahia_principe_10-31.jsonl")
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bahia_principe_debug_out")


def normalize_hotel_name(name):
    """Bahia Principe's own directory names are already clean — the only
    difference from Southwest's decorated strings (confirmed live against
    all 10 Southwest-listed hotels) is accents (í/á) and a trailing "+18"/
    "+16" adults-only/age-restriction suffix Southwest's own names drop."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"\s*\+\d+\s*$", "", name)
    return re.sub(r"\s+", " ", name).strip().lower()


def match_southwest_name(candidate_name, southwest_names):
    normalized_to_original = {normalize_hotel_name(n): n for n in southwest_names}
    return normalized_to_original.get(normalize_hotel_name(candidate_name))


def load_bahia_principe_hotels_by_destination():
    by_destination = {}
    for path in glob.glob(os.path.join(DATA_DIR, "hotels_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                hotel = json.loads(line)
                folded = unicodedata.normalize("NFKD", hotel["name"]).encode("ascii", "ignore").decode("ascii")
                if re.search(r"bahia?\s*princip", folded, re.IGNORECASE):
                    by_destination.setdefault(hotel["destination"], []).append(hotel["name"])
    return by_destination


async def fetch_directory(page):
    """The full worldwide hotel directory, read straight off the homepage's
    own destination-search buttons — no typing or panel-opening needed
    (confirmed live: all 22 button[data-hotel-id] elements are already
    present in the raw DOM on load)."""
    await page.goto(URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    try:
        await page.get_by_role("button", name="GOT IT").click(timeout=3000)
        await page.wait_for_timeout(500)
    except Exception:
        pass  # cookie banner already dismissed / didn't appear this time

    hotels = await page.evaluate(
        """() => Array.from(document.querySelectorAll('button[data-hotel-id]')).map(b => ({
            name: b.textContent.trim(),
            roibackCode: b.getAttribute('data-roibackcode'),
        }))"""
    )
    return hotels


def resolve_bahia_principe_hotels(by_destination, directory):
    """Maps each Southwest-tagged hotel to its Roiback booking code, resolved
    entirely from the directory fetched above — no live searching needed.
    Returns [(southwest_destination, southwest_hotel_name, roiback_code, directory_name), ...]."""
    name_to_code = {h["name"]: h["roibackCode"] for h in directory}
    directory_names = list(name_to_code.keys())

    resolved = []
    for southwest_destination, hotel_names in by_destination.items():
        for name in hotel_names:
            matched_title = match_southwest_name(name, directory_names)
            if matched_title is None:
                print(f"WARNING: {name!r} not found in Bahia Principe's own hotel directory — skipping")
                continue
            resolved.append((southwest_destination, name, name_to_code[matched_title], matched_title))
    return resolved


async def scope_bahia_principe():
    by_destination = load_bahia_principe_hotels_by_destination()
    print(f"Bahia Principe hotels found in scraped jsonl data: {by_destination}")

    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = start_atomic_write(OUTPUT_PATH)
    scraped_at = datetime.date.today().isoformat()
    written = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        directory = await fetch_directory(page)
        resolved = resolve_bahia_principe_hotels(by_destination, directory)
        print(f"Resolved {len(resolved)} hotels")

        # Reuses palladium_script.py's exact room-page parser — confirmed
        # live that Bahia Principe's booking pages (same Roiback platform)
        # respond to its data-testid selectors unmodified.
        from palladium_script import parse_palladium_room_page

        for sw_dest, sw_name, code, directory_name in resolved:
            room_url = BOOKING_URL_TEMPLATE.format(code=code, check_in=CHECK_IN_DATE_ATTR, check_out=CHECK_OUT_DATE_ATTR)
            try:
                await human_pause()
                await page.goto(room_url, wait_until="domcontentloaded")
                await page.locator('[data-testid="fn-desktop-availability-page-room"]').first.wait_for(state="visible", timeout=20000)
                await page.wait_for_timeout(1000)
                rooms = await parse_palladium_room_page(page)
            except Exception as e:
                print(f"WARNING: failed to load room detail for {sw_name!r} ({code}): {type(e).__name__}: {e}")
                os.makedirs(DEBUG_DIR, exist_ok=True)
                safe_name = re.sub(r"\W+", "_", code)
                await page.screenshot(path=os.path.join(DEBUG_DIR, f"{safe_name}_room_page_fail.png"), full_page=True)
                rooms = []

            all_prices = [ro["price"] for room in rooms for ro in room["rate_options"] if ro["price"] is not None]
            listing_price = min(all_prices) if all_prices else None
            print(f"[{sw_dest}] {directory_name!r} — {len(rooms)} room(s), listing_price={listing_price}")

            record = {
                "chain": "bahia_principe",
                "destination": sw_dest,
                "southwest_hotel_name": sw_name,
                "bahia_principe_name": directory_name,
                "departure_date": DEPARTURE_DATE,
                "return_date": RETURN_DATE,
                "scraped_at": scraped_at,
                "listing_price": listing_price,
                "currency": "USD",
                "booking_url": room_url,
                "rooms": rooms,
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
    asyncio.run(scope_bahia_principe())
