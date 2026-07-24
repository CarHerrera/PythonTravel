from pydantic import BaseModel

class Destination(BaseModel):
    name: str
    country: str
    airport_code: str
    has_beach: bool = True