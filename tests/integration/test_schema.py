"""The migration enforces the storage-level invariants from section 4."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


async def _payment(conn: AsyncConnection, key: str = "k1", amount: int = 1000) -> uuid.UUID:
    pid = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO payments (id, idempotency_key, request_hash, amount_minor, currency,"
            " pm_token, status, deadline_at)"
            " VALUES (:id, :k, 'h', :a, 'EUR', 'tok', 'CREATED', now() + interval '20 s')"
        ),
        {"id": pid, "k": key, "a": amount},
    )
    return pid


async def _attempt(conn: AsyncConnection, pid: uuid.UUID, seq: int, status: str) -> None:
    await conn.execute(
        text(
            "INSERT INTO attempts (id, payment_id, seq, provider, status)"
            " VALUES (:id, :p, :s, 'mock_a', :st)"
        ),
        {"id": uuid.uuid4(), "p": pid, "s": seq, "st": status},
    )


async def test_at_most_one_succeeded_attempt(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        pid = await _payment(conn)
        await _attempt(conn, pid, 1, "SUCCEEDED")
        await _attempt(conn, pid, 2, "DECLINED_SOFT")
    with pytest.raises(IntegrityError, match="uq_attempts_one_success"):
        async with engine.begin() as conn:
            await _attempt(conn, pid, 3, "SUCCEEDED")


async def test_refunded_duplicate_does_not_block_success(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        pid = await _payment(conn)
        await _attempt(conn, pid, 1, "REFUNDED_DUPLICATE")
        await _attempt(conn, pid, 2, "SUCCEEDED")


async def test_attempt_seq_unique(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        pid = await _payment(conn)
        await _attempt(conn, pid, 1, "STARTED")
    with pytest.raises(IntegrityError, match="uq_attempts_payment_seq"):
        async with engine.begin() as conn:
            await _attempt(conn, pid, 1, "STARTED")


async def test_idempotency_key_unique(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await _payment(conn, key="same")
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await _payment(conn, key="same")


@pytest.mark.parametrize("amount", [0, -5])
async def test_amount_positive(engine: AsyncEngine, amount: int) -> None:
    with pytest.raises(IntegrityError, match="ck_payments_amount_positive"):
        async with engine.begin() as conn:
            await _payment(conn, amount=amount)


async def test_webhook_event_dedup(engine: AsyncEngine) -> None:
    sql = text(
        "INSERT INTO webhook_events (provider, event_id, payload)"
        " VALUES ('mock_a', 'e1', '{}'::jsonb) ON CONFLICT DO NOTHING"
    )
    async with engine.begin() as conn:
        first = await conn.execute(sql)
        second = await conn.execute(sql)
    assert first.rowcount == 1
    assert second.rowcount == 0


async def test_enum_values_match_domain(engine: AsyncEngine) -> None:
    from cascade.domain.enums import AttemptStatus, PaymentStatus

    async with engine.connect() as conn:
        for typ, enum in (("payment_status", PaymentStatus), ("attempt_status", AttemptStatus)):
            rows = await conn.execute(text(f"SELECT unnest(enum_range(NULL::{typ}))::text"))
            assert {r[0] for r in rows} == {m.value for m in enum}


def test_models_match_migration() -> None:
    """`alembic check`: ORM models and the migration describe the same schema."""
    import subprocess
    import sys

    res = subprocess.run(
        [sys.executable, "-m", "alembic", "check"], capture_output=True, text=True, check=False
    )
    assert res.returncode == 0, res.stdout + res.stderr
