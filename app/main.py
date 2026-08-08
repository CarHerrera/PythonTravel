from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.config import settings
from app.routers import southwest, rates, flights
app = FastAPI(title=settings.app_name)

app.include_router(southwest.router)
app.include_router(rates.router)
app.include_router(flights.router)
app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")

@app.get("/")
def read_root():
    return {"status": "ok"}