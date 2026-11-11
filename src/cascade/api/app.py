import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from cascade.api.routes import router
from cascade.infrastructure.container import Services, build_services
from cascade.infrastructure.logging import setup_logging
from cascade.infrastructure.settings import get_settings


def create_app(services: Services | None = None) -> FastAPI:
    """Production: services=None builds everything from env + config/routing.yaml
    (invalid config fails here, at startup). Tests pass their own Services."""
    settings = get_settings()
    setup_logging(settings.log_level)
    owned = services is None

    if services is None:
        from cascade.infrastructure.tasks import TaskiqQueue, broker

        services = build_services(settings, queue=TaskiqQueue())

        @contextlib.asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            await broker.startup()
            yield
            await broker.shutdown()
            await services.aclose()

    else:

        @contextlib.asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            yield

    app = FastAPI(title="Payment Cascade", lifespan=lifespan)
    app.state.services = services
    app.state.owned = owned
    app.include_router(router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn cascade.api.app:app` builds the production app lazily, so importing this
    # module in tests does not need a database or the routing config.
    if name == "app":
        return create_app()
    raise AttributeError(name)
