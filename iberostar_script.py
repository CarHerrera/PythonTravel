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

URL = "https://www.iberostar.com/en/"
DIRECTORY_URL = "https://www.iberostar.com/en/ajax_contents/fastbooking_data/?v=16&market_id=7"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "data")

DEPARTURE_DATE = "10/31"
RETURN_DATE = "11/07"
OUTPUT_PATH = os.path.join(DATA_DIR, "rates_iberostar_10-31.jsonl")
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iberostar_debug_out")

# Confirmed live: iberostar.com changed its search behavior — typing a
# hotel's own exact name into the destination box (the whole basis of the
# previous per-hotel-search design) now lands on that hotel's marketing page
# instead of redirecting to booking, breaking every hotel identically. But
# the *destination directory* (fetch_directory below) already carries each
# hotel's own numeric id ("id": "h75" -> 75), and that id alone is enough to
# build this URL directly — confirmed live it 302s straight to
# booking.iberostar.com/Reservations/Availability with the right hotel and
# dates pre-filled, the exact same page parse_single_hotel_availability_page
# already parses. No destination typing, no calendar widget, no results
# listing to click through — this replaces all of that.
BOOKING_URL_TEMPLATE = (
    "https://www.iberostar.com/en/bookings/?currency_code=USD"
    "&vo_booking%5Bcheck_in_date%5D={check_in}"
    "&vo_booking%5Bcheck_out_date%5D={check_out}"
    "&vo_booking%5Bhotel_id%5D={hotel_id}&activeMiIB=0"
)
CHECK_IN_DATE_URL = "10%2F31%2F2026"
CHECK_OUT_DATE_URL = "11%2F07%2F2026"

# Southwest decorates Iberostar names with suffixes Iberostar's own
# directory doesn't have (e.g. Southwest: "Iberostar Selection Cancun All
# Inclusive" vs Iberostar's own "Iberostar Selection Cancún") — stripped
# before comparing. Accents are folded too (Cancún/Bávaro vs Cancun/Bavaro).
DECORATION_RE = re.compile(r"\s*-?\s*(all inclusive|adults only|ai)\b", re.IGNORECASE)


def normalize_hotel_name(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = DECORATION_RE.sub("", name)
    return re.sub(r"\s+", " ", name).strip().lower()


def match_southwest_name(candidate_name, southwest_names):
    """candidate_name is what Iberostar's own site calls a hotel (from
    either the directory or a search-results price entry); southwest_names
    are Southwest's decorated strings for the same destination. Returns the
    matching Southwest name (the join key) or None.

    Every genuine match found so far normalizes to the exact same string on
    both sides (Iberostar's decoration-stripping is clean, unlike
    Palladium's messier truncation) — so this requires exact equality, not
    a ratio-based fuzzy match. A fuzzy ratio was tried first and confirmed
    live to be unsafe here: "Iberostar Selection Bavaro" scored high enough
    against the unrelated "Iberostar Selection Coral Bávaro" to pass,
    because "Coral" is the only extra word out of four. The one thing a
    ratio approach was for — tolerating Southwest's own leftover truncation
    fragments (e.g. a trailing "all inclusi" that didn't fully strip) — is
    covered instead by a prefix check: real truncation is always trailing,
    so the shorter name is always a clean PREFIX of the longer one. A word
    inserted anywhere but the very end, like "Coral", breaks prefix
    matching immediately — which is exactly the rejection that's needed."""
    normalized_to_original = {normalize_hotel_name(n): n for n in southwest_names}
    target = normalize_hotel_name(candidate_name)
    for norm_name, original in normalized_to_original.items():
        if target == norm_name or target.startswith(norm_name) or norm_name.startswith(target):
            return original
    return None


def load_iberostar_hotels_by_destination():
    """Read app/data/hotels_*.jsonl directly — not via app.services.southwest
    — so this scraper stays independent of the FastAPI app. Groups
    Iberostar-branded entries (including its JOIA/Waves/Selection
    sub-brands) by Southwest's destination tag, since that's the join key
    the FastAPI app's rates service keys off of."""
    by_destination = {}
    for path in glob.glob(os.path.join(DATA_DIR, "hotels_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                hotel = json.loads(line)
                if re.search(r"\biberostar\b|\bjoia\b", hotel["name"], re.IGNORECASE):
                    by_destination.setdefault(hotel["destination"], []).append(hotel["name"])
    return by_destination


async def fetch_directory(page):
    """Iberostar's homepage loads a single ~500KB JSON blob (confirmed live)
    listing every hotel and destination it has worldwide, with each hotel's
    own destination id. Fetching it directly here is far more reliable than
    reverse-engineering the destination picker's autocomplete, and is what
    lets us resolve precisely which of Iberostar's own destinations (much
    more granular than Southwest's — e.g. most of Southwest's "Cancun"
    hotels are actually grouped under "Playa Paraíso"/"Cozumel"/"Puerto
    Morelos" on Iberostar's own site; confirmed live that searching "Cancun"
    itself only surfaces 2 of the 9) each target hotel actually lives
    under, entirely from data instead of trial-and-error searching.

    Fetched via in-page fetch(), not page.request — the latter is a
    separate lower-level HTTP client that doesn't carry the real browser's
    fingerprint the way actual page navigation does (same root cause as the
    Akamai block we hit with headless/plain curl earlier), and gets an
    Access Denied HTML page back instead of JSON."""
    await page.goto(URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    try:
        await page.locator("#onetrust-accept-btn-handler").click(force=True, timeout=3000)
        await page.wait_for_timeout(500)
    except Exception:
        pass

    body = await page.evaluate(
        """async (url) => {
            const res = await fetch(url);
            return await res.json();
        }""",
        DIRECTORY_URL,
    )
    dest_by_id = {d["id"]: d["title"] for d in body["destinations"][1]}
    hotel_directory = body["hotels"][1]
    return dest_by_id, hotel_directory


def resolve_iberostar_hotel_names(iberostar_by_destination, hotel_directory):
    """Maps each Southwest-tagged Iberostar hotel to its Iberostar directory
    entry — resolved by name (via match_southwest_name, with its
    prefix-matching safety net; a raw difflib call is exactly what let
    "Iberostar Selection Bavaro" silently resolve to the unrelated
    "Iberostar Selection Coral Bávaro" before that safety net existed) —
    and, from that entry, its numeric hotel id, e.g. "id": "h75" -> "75".
    That id is all scope_iberostar needs to build a direct booking URL, no
    further searching required. Returns
    [(southwest_destination, southwest_hotel_name, iberostar_title, hotel_id), ...]."""
    directory_titles = [h["title"] for h in hotel_directory]
    title_to_entry = {h["title"]: h for h in hotel_directory}

    resolved = []
    for southwest_destination, hotel_names in iberostar_by_destination.items():
        for name in hotel_names:
            matched_title = match_southwest_name(name, directory_titles)
            if matched_title is None:
                print(f"WARNING: {name!r} not found in Iberostar's own hotel directory — skipping")
                continue
            entry = title_to_entry[matched_title]
            hotel_id = entry["id"].lstrip("h")
            resolved.append((southwest_destination, name, entry["title"], hotel_id))
    return resolved


def _parse_iberostar_price(text):
    """'USD\xa05,052' (a real non-breaking space from &nbsp;) -> 5052.0."""
    if not text:
        return None
    cleaned = text.replace("USD", "").replace("\xa0", "").replace(",", "").strip()
    return float(cleaned) if cleaned else None


# Confirmed live on the single-hotel room-selection page — room size is
# reported in square feet there ("592ft2"), not square meters like RIU's
# pages, so it's converted for consistency with the shared schema's
# size_sq_m field.
SIZE_SQFT_RE = re.compile(r"(\d+)\s*ft2", re.IGNORECASE)
SQFT_TO_SQM = 0.092903


async def parse_single_hotel_availability_page(page):
    """A destination that resolves to exactly one hotel skips the results
    list/availability-job entirely and redirects straight to that hotel's
    own room-selection page (confirmed live: Aruba/"Eagle Beach", 1 target
    hotel) — fully server/client-rendered with no separate JSON API, so
    it's parsed directly from the DOM instead of polled for a job."""
    name = (await page.locator(".sticky-hotel-title").first.inner_text()).strip()

    rooms = []
    room_wrappers = page.locator(".room-list > div")
    room_count = await room_wrappers.count()
    for i in range(room_count):
        wrapper = room_wrappers.nth(i)
        try:
            room_name = (await wrapper.locator(".card-title.roomName").first.inner_text()).strip()

            service_texts = await wrapper.locator(".serviceTitle").all_inner_texts()
            size_sq_m = None
            amenities = []
            for t in service_texts:
                t = t.strip()
                m = SIZE_SQFT_RE.search(t)
                if m and size_sq_m is None:
                    size_sq_m = round(int(m.group(1)) * SQFT_TO_SQM)
                elif t:
                    amenities.append(t)

            images = await wrapper.locator("img.lzy_room_img").evaluate_all(
                "els => els.map(e => e.getAttribute('src')).filter(Boolean)"
            )
            images = list(dict.fromkeys(images))  # de-dupe, preserve order — swiper repeats slide images

            rate_options = []
            rate_cards = wrapper.locator(".b-rate-card")
            for j in range(await rate_cards.count()):
                rate_card = rate_cards.nth(j)

                title_el = rate_card.locator(".rate-title-room")
                pill = (await title_el.first.inner_text()).strip() if await title_el.count() else ""

                new_price_el = rate_card.locator(".new-price")
                price = _parse_iberostar_price(await new_price_el.first.inner_text()) if await new_price_el.count() else None

                old_price_el = rate_card.locator(".old-price")
                original_price = _parse_iberostar_price(await old_price_el.first.inner_text()) if await old_price_el.count() else None

                rate_options.append({
                    "rate_id": "",
                    "price": price,
                    "original_price": original_price,
                    # Not shown directly on this page — derived from the
                    # fixed 7-night stay this whole script scopes to.
                    "price_per_night": round(price / 7, 2) if price else None,
                    "pills": [pill] if pill else [],
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
            print(f"    failed to read room {i} on single-hotel page for {name!r}: {e}")

    all_prices = [ro["price"] for room in rooms for ro in room["rate_options"] if ro["price"] is not None]
    listing_price = min(all_prices) if all_prices else None

    return {"name": name, "listing_price": listing_price, "rooms": rooms}


async def scope_iberostar():
    iberostar_by_destination = load_iberostar_hotels_by_destination()
    print(f"Iberostar hotels found in scraped jsonl data: {iberostar_by_destination}")

    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = start_atomic_write(OUTPUT_PATH)
    scraped_at = datetime.date.today().isoformat()
    written = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        _, hotel_directory = await fetch_directory(page)
        resolved = resolve_iberostar_hotel_names(iberostar_by_destination, hotel_directory)
        print(f"Resolved {len(resolved)} hotels")

        for sw_dest, sw_name, iberostar_title, hotel_id in resolved:
            booking_url = BOOKING_URL_TEMPLATE.format(
                check_in=CHECK_IN_DATE_URL, check_out=CHECK_OUT_DATE_URL, hotel_id=hotel_id
            )
            try:
                await human_pause()
                await page.goto(booking_url, wait_until="domcontentloaded", timeout=30000)
                await page.locator(".room-list > div").first.wait_for(state="visible", timeout=20000)
                await page.wait_for_timeout(1000)
                hotel = await parse_single_hotel_availability_page(page)
            except Exception as e:
                print(f"!!! FAILED hotel={iberostar_title!r} (id={hotel_id}): {type(e).__name__}: {e}")
                os.makedirs(DEBUG_DIR, exist_ok=True)
                safe_name = re.sub(r"\W+", "_", iberostar_title)
                try:
                    await page.screenshot(path=os.path.join(DEBUG_DIR, f"{safe_name}_room_page_fail.png"), full_page=True)
                except Exception:
                    pass
                continue

            iberostar_name = hotel["name"]
            if not iberostar_name:
                print(f"  no hotel name parsed for id={hotel_id} ({iberostar_title!r}) — skipping")
                continue

            # Only one candidate here (this URL is scoped to exactly this
            # hotel_id) — still goes through match_southwest_name rather
            # than assuming a match, as a sanity check against a bad id
            # mapping silently attaching the wrong hotel's rooms.
            matched = match_southwest_name(iberostar_name, [sw_name])
            if matched is None:
                print(f"  no Southwest match for Iberostar listing {iberostar_name!r} (expected {sw_name!r}) — skipping")
                continue

            print(f"[{sw_dest}] {iberostar_name!r} — {len(hotel['rooms'])} room(s)")
            record = {
                "chain": "iberostar",
                "destination": sw_dest,
                "southwest_hotel_name": matched,
                "iberostar_name": iberostar_name,
                "departure_date": DEPARTURE_DATE,
                "return_date": RETURN_DATE,
                "scraped_at": scraped_at,
                "listing_price": hotel["listing_price"],
                "currency": "USD",
                "booking_url": booking_url,
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
    asyncio.run(scope_iberostar())
