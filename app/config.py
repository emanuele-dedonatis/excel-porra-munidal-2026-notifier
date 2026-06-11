from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str = ""
    football_data_api_key: str = ""
    database_url: str = "sqlite:///./data/app.db"
    poll_interval_seconds: int = 300
    debug: bool = False

    model_config = {"env_file": ".env"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
