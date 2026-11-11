from fastapi import FastAPI

from cascade.infrastructure.logging import setup_logging
from cascade.infrastructure.settings import get_settings


def create_app() -> FastAPI:
    setup_logging(get_settings().log_level)
    app = FastAPI(title="Payment Cascade")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
