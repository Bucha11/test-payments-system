import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DATABASE_URL = os.environ.get("DATABASE_URL")


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set (run via `make test`)")
    eng = create_async_engine(DATABASE_URL)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
async def clean_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE payments, attempts, webhook_events CASCADE"))
