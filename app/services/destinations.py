import json
from pathlib import Path
from app.schemas.destination import Destination

DATA_FILE = Path(__file__).parent.parent / "data" / "resorts.json"

def _load() -> list[Destination]:
    with open(DATA_FILE) as f:
        raw = json.load(f)
    return [Destination(**item) for item in raw]

_DESTINATIONS = _load()

def get_all() -> list[Destination]:
    return _DESTINATIONS

def get_by_name(name: str) -> Destination | None:
    for dest in _DESTINATIONS:
        if dest.name.lower() == name.lower():
            return dest
    return None