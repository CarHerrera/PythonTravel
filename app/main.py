from fastapi import FastAPI
from app.config import settings
from app.routers import destinations

app = FastAPI(title=settings.app_name)

app.include_router(destinations.router)

@app.get("/")
def read_root():
    return {"status": "ok"}