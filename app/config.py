from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    app_name: str = "Travel API"
    debug: bool = False
    amadeus_api_key: str = ""

    class Config:
        env_file = ".env"

settings = Settings()