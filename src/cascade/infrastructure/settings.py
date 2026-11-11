from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://cascade:cascade@localhost:5432/cascade"
    redis_url: str = "redis://localhost:6379/0"
    routing_config: str = "config/routing.yaml"
    webhook_secret: str = "dev-webhook-secret"
    log_level: str = "INFO"
    # Braintree (optional). Empty credentials: the adapter is not built, the provider
    # never enters a route.
    braintree_environment: str = "sandbox"
    braintree_merchant_id: str = ""
    braintree_public_key: str = ""
    braintree_private_key: str = ""

    @property
    def braintree_configured(self) -> bool:
        return bool(
            self.braintree_merchant_id and self.braintree_public_key and self.braintree_private_key
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
