from fastapi import APIRouter
from app.schemas.rates import ChainRate
from app.services import rates as service

router = APIRouter(prefix="/rates")

@router.get("/", response_model=list[ChainRate])
def list_rates():
    return service.get_all()

@router.get("/by-chain/{chain}", response_model=list[ChainRate])
def list_by_chain(chain: str):
    return service.get_by_chain(chain)

@router.get("/by-destination/{destination}", response_model=list[ChainRate])
def list_by_destination(destination: str):
    return service.get_by_destination(destination)
