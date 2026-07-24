from pydantic import BaseModel

class Destination(BaseModel):
    name: str
    country: str
    has_beach: bool = True