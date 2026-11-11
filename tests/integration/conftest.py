import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cascade.application.config import RoutingConfig
from tests.integration.harness import Harness, make_harness

DATABASE_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


def _require_db() -> str:
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set (run via `make test`)")
    return DATABASE_URL


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(_require_db())
    yield eng
    await eng.dispose()


@pytest.fixture
async def clean_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE payments, attempts, webhook_events CASCADE"))


HarnessFactory = Callable[..., Awaitable[Harness]]


@pytest.fixture
async def make_env() -> AsyncIterator[HarnessFactory]:
    url = _require_db()
    made: list[Harness] = []

    async def factory(config: RoutingConfig | None = None, **_: Any) -> Harness:
        h = await make_harness(url, REDIS_URL, config)
        made.append(h)
        return h

    yield factory
    for h in made:
        await h.close()


@pytest.fixture
async def env(make_env: HarnessFactory) -> Harness:
    return await make_env()
