from fastapi import APIRouter, HTTPException
from app.schemas.flights import FlightPrice
from app.services import flights as service

router = APIRouter(prefix="/flights")

@router.get("/", response_model=list[FlightPrice])
def list_flights():
    return service.get_all()

@router.get("/by-destination/{destination}", response_model=FlightPrice)
def get_by_destination(destination: str):
    flight = service.get_by_destination(destination)
    if flight is None:
        raise HTTPException(status_code=404, detail="No flight price entered for this destination yet")
    return flight
