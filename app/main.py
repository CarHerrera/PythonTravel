from fastapi import FastAPI
from app.config import settings
from app.routers import destinations, southwest
app = FastAPI(title=settings.app_name)

app.include_router(destinations.router)
app.include_router(southwest.router)

@app.get("/")
def read_root():
    return {"status": "ok"}