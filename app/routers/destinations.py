from fastapi import APIRouter, HTTPException
from app.schemas.destination import Destination
from app.services import destinations as service

router = APIRouter(prefix="/destinations", tags=["destinations"])

@router.get("/", response_model=list[Destination])
def list_destinations():
    return service.get_all()

@router.get("/{name}", response_model=Destination)
def get_destination(name: str):
    dest = service.get_by_name(name)
    if dest is None:
        raise HTTPException(status_code=404, detail="Destination not found")
    return dest