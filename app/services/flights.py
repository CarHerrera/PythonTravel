import json
from pathlib import Path
from app.schemas.flights import FlightPrice

DATA_FOLDER = Path(__file__).parent.parent / "data"
PREFIX = "flights_"


def _load() -> list[FlightPrice]:
    raw_out = []
    for file_path in DATA_FOLDER.glob(f"{PREFIX}*"):
        if file_path.is_file():
            with file_path.open() as file:
                for line in file:
                    line = line.strip()
                    if not line:
                        continue
                    raw_out.append(FlightPrice(**json.loads(line)))
    return raw_out


_FLIGHTS = _load()
_BY_DESTINATION: dict[str, FlightPrice] = {f.destination: f for f in _FLIGHTS if f.price_per_person is not None}


def get_all() -> list[FlightPrice]:
    return _FLIGHTS


def get_by_destination(destination: str) -> FlightPrice | None:
    return _BY_DESTINATION.get(destination)
