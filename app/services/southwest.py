import json
from pathlib import Path
from app.schemas.southwest import SouthwestHotel

DATA_FOLDER = Path(__file__).parent.parent / "data"
PREFIX = "hotels_"


def _normalize(raw: dict) -> dict:
    """Scraped rows can have null amenities/room_types when a hotel's detail
    or room-options fetch didn't fully capture — treat missing as empty."""
    raw["amenities"] = raw.get("amenities") or []
    raw["images"] = raw.get("images") or []
    raw["room_types"] = raw.get("room_types") or []
    for room_type in raw["room_types"]:
        room_type["amenities"] = room_type.get("amenities") or []
        room_type["images"] = room_type.get("images") or []
    return raw


def _load() -> list[SouthwestHotel]:
    raw_out = []
    for file_path in DATA_FOLDER.glob(f"{PREFIX}*"):
        if file_path.is_file():
            with file_path.open() as file:
                for line in file:
                    line = line.strip()
                    if not line:
                        continue
                    raw_out.append(SouthwestHotel(**_normalize(json.loads(line))))
    return raw_out


_SOUTHWEST_VACATIONS = _load()


def get_all() -> list[SouthwestHotel]:
    return _SOUTHWEST_VACATIONS

def _parse_cost(base_cost: str | None) -> float | None:
    if base_cost is None:
        return None
    return float(base_cost.replace("$", "").replace(",", ""))


def get_by_range(min_cost: float, max_cost: float) -> list[SouthwestHotel]:
    result = []
    for hotel in _SOUTHWEST_VACATIONS:
        cost = _parse_cost(hotel.base_cost)
        if cost is not None and min_cost <= cost <= max_cost:
            result.append(hotel)
    return result