from pydantic import BaseModel
from typing import Optional
from datetime import date


class ChainRateOption(BaseModel):
    rate_id: Optional[str] = None
    price: Optional[float] = None
    original_price: Optional[float] = None
    price_per_night: Optional[float] = None
    pills: list[str] = []
    currency: Optional[str] = None


class ChainRoom(BaseModel):
    name: Optional[str] = None
    size_sq_m: Optional[int] = None
    amenities: list[str] = []
    images: list[str] = []
    rate_options: list[ChainRateOption] = []


class ChainRate(BaseModel):
    """One chain's scraped rate for one Southwest-listed hotel, for one date
    range. Deliberately generic — `chain` distinguishes RIU from any future
    scraper writing the same shape (e.g. rates_iberostar_*.jsonl); no
    per-chain fields (like RIU's own `riu_name`) are modeled here, since
    they aren't part of the shared contract and Pydantic drops them."""
    chain: str
    destination: str
    southwest_hotel_name: str
    departure_date: str
    return_date: str
    scraped_at: date
    listing_price: Optional[float] = None
    currency: Optional[str] = None
    booking_url: Optional[str] = None
    rooms: list[ChainRoom] = []
