import json
from collections import defaultdict
from pathlib import Path
from app.schemas.rates import ChainRate

DATA_FOLDER = Path(__file__).parent.parent / "data"
# Extension included (unlike southwest.py's looser "hotels_*" glob) so a
# mid-scrape ".part" temp file (scraping_common.start_atomic_write) never
# matches.
GLOB_PATTERN = "rates_*.jsonl"


def _normalize(raw: dict) -> dict:
    """Chain scrapers can null out nested lists the same way southwest_script
    does when a fetch didn't fully capture — coalesce to empty rather than
    letting Pydantic reject the whole record."""
    raw["rooms"] = raw.get("rooms") or []
    for room in raw["rooms"]:
        room["amenities"] = room.get("amenities") or []
        room["images"] = room.get("images") or []
        room["rate_options"] = room.get("rate_options") or []
        for rate_option in room["rate_options"]:
            rate_option["pills"] = rate_option.get("pills") or []
    # The frontend join is exact-string on these two fields — trim
    # defensively so a future scraper's stray whitespace can't silently
    # break the match.
    raw["destination"] = raw["destination"].strip()
    raw["southwest_hotel_name"] = raw["southwest_hotel_name"].strip()
    return raw


def _load() -> list[ChainRate]:
    raw_out = []
    for file_path in DATA_FOLDER.glob(GLOB_PATTERN):
        if file_path.is_file():
            with file_path.open() as file:
                for line in file:
                    line = line.strip()
                    if not line:
                        continue
                    raw_out.append(ChainRate(**_normalize(json.loads(line))))
    return raw_out


_CHAIN_RATES = _load()

# Keyed by (destination, southwest_hotel_name) -> list[ChainRate]. A list,
# not a single ChainRate, since nothing prevents two chains (or two scraper
# runs) from matching the same Southwest hotel.
_BY_HOTEL_KEY: dict[tuple[str, str], list[ChainRate]] = defaultdict(list)
for _rate in _CHAIN_RATES:
    _BY_HOTEL_KEY[(_rate.destination, _rate.southwest_hotel_name)].append(_rate)


def get_all() -> list[ChainRate]:
    return _CHAIN_RATES


def get_by_chain(chain: str) -> list[ChainRate]:
    return [r for r in _CHAIN_RATES if r.chain.lower() == chain.lower()]


def get_by_destination(destination: str) -> list[ChainRate]:
    return [r for r in _CHAIN_RATES if r.destination.lower() == destination.lower()]


def get_for_hotel(destination: str, southwest_hotel_name: str) -> list[ChainRate]:
    return _BY_HOTEL_KEY.get((destination.strip(), southwest_hotel_name.strip()), [])
