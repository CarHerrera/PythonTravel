from fastapi import APIRouter, HTTPException
from app.schemas.southwest import SouthwestHotel
from app.services import southwest as service

router = APIRouter(prefix="/southwestvacations")

@router.get("/", response_model=list[SouthwestHotel])
def list_destinations():
    return service.get_all()

@router.get("/range", response_model=list[SouthwestHotel])
def get_range(min: int, max: int):
    range = service.get_by_range(min,max)
    if range is None:
        raise HTTPException(status_code=404, detail="Could not find any")
    return range