from pydantic import BaseModel
from typing import Optional
from datetime import date


class SouthwestRoomType(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    amenities: list[str] = []
    images: list[str] = []
    price_difference: Optional[float] = None
    total_price: Optional[float] = None
    currency: Optional[str] = None


class SouthwestHotel(BaseModel):
    origin: str
    destination: str
    departure_date: str
    return_date: str
    scraped_at: date
    base_cost: Optional[str] = None
    name: str
    description: Optional[str] = None
    star_rating: Optional[float] = None
    amenities: list[str] = []
    room_types: list[SouthwestRoomType] = []