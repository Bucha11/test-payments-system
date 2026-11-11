from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://cascade:cascade@localhost:5432/cascade"
    redis_url: str = "redis://localhost:6379/0"
    routing_config: str = "config/routing.yaml"
    webhook_secret: str = "dev-webhook-secret"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
