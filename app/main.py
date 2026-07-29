from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.config import settings
from app.routers import destinations, southwest
app = FastAPI(title=settings.app_name)

app.include_router(destinations.router)
app.include_router(southwest.router)
app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")

@app.get("/")
def read_root():
    return {"status": "ok"}