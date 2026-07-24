from fastapi import FastAPI
from app.routers import destinations

app = FastAPI(title="Travel API")

app.include_router(destinations.router)

@app.get("/")
def read_root():
    return {"status": "ok"}