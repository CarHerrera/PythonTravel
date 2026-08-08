from pydantic import BaseModel
from typing import Optional
from datetime import date


class FlightPrice(BaseModel):
    """A hand-entered Southwest fare for one BWI -> destination round trip.

    Not scraped: southwest.com/air/booking/'s own shopping API returns a
    403 whose body states automated access to fare data is against
    Southwest's Terms & Conditions (confirmed live, persists even headed) —
    so this is filled in manually instead, same shape as the scraped
    rates_*.jsonl files so the rest of the app treats it identically.
    price_per_person is round-trip; the UI multiplies by passengers (2, matching
    the party size every chain scraper searches with) to get a comparable total.
    """
    origin: str
    destination: str
    departure_date: str
    return_date: str
    price_per_person: Optional[float] = None
    passengers: int = 2
    currency: str = "USD"
    entered_at: Optional[date] = None
    notes: Optional[str] = None
