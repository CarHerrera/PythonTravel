from app.schemas.destination import Destination

_DESTINATIONS = [
    Destination(name="Aruba", country="Aruba"),
    Destination(name="Belize City", country="Belize"),
]

def get_all() -> list[Destination]:
    return _DESTINATIONS

def get_by_name(name: str) -> Destination | None:
    for dest in _DESTINATIONS:
        if dest.name.lower() == name.lower():
            return dest
    return None