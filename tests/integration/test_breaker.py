import pytest
from redis.asyncio import Redis

from cascade.domain.enums import BreakerState, ChargeOutcome
from cascade.infrastructure.breaker import BreakerPolicy, RedisCircuitBreaker
from tests.integration.conftest import REDIS_URL
from tests.integration.harness import FakeClock

pytestmark = pytest.mark.integration

U, F, OK, SOFT, HARD = (
    ChargeOutcome.UNKNOWN,
    ChargeOutcome.FAILED_NOT_CHARGED,
    ChargeOutcome.APPROVED,
    ChargeOutcome.SOFT_DECLINE,
    ChargeOutcome.HARD_DECLINE,
)


@pytest.fixture
async def br() -> tuple[RedisCircuitBreaker, FakeClock]:
    r = Redis.from_url(REDIS_URL)
    await r.flushdb()
    clock = FakeClock()
    return RedisCircuitBreaker(r, {"p": BreakerPolicy(3, 30)}, clock=clock.ts), clock


async def test_opens_after_consecutive_failures(br: tuple[RedisCircuitBreaker, FakeClock]) -> None:
    b, _ = br
    for o in (U, F):
        await b.record("p", o)
    assert (await b.snapshot("p")).state is BreakerState.CLOSED
    await b.record("p", U)
    snap = await b.snapshot("p")
    assert snap.state is BreakerState.OPEN
    assert snap.seconds_until_half_open is not None and 29 < snap.seconds_until_half_open <= 30
    assert not await b.allow("p")


async def test_declines_and_success_reset_count(br: tuple[RedisCircuitBreaker, FakeClock]) -> None:
    b, _ = br
    for o in (U, U, SOFT, U, U, HARD, U, U, OK, U, U):
        await b.record("p", o)
    snap = await b.snapshot("p")
    assert snap.state is BreakerState.CLOSED
    assert snap.consecutive_failures == 2


async def test_half_open_single_probe(br: tuple[RedisCircuitBreaker, FakeClock]) -> None:
    b, clock = br
    for _ in range(3):
        await b.record("p", U)
    clock.advance(30)
    assert (await b.snapshot("p")).state is BreakerState.HALF_OPEN
    assert await b.allow("p")  # the probe
    assert not await b.allow("p")  # only one
    await b.record("p", OK)
    assert (await b.snapshot("p")).state is BreakerState.CLOSED
    assert await b.allow("p")


async def test_half_open_failure_reopens(br: tuple[RedisCircuitBreaker, FakeClock]) -> None:
    b, clock = br
    for _ in range(3):
        await b.record("p", F)
    clock.advance(31)
    assert await b.allow("p")
    await b.record("p", U)
    assert (await b.snapshot("p")).state is BreakerState.OPEN
    clock.advance(31)
    assert await b.allow("p")  # next cooldown, next single probe


async def test_redis_down_fails_open() -> None:
    dead = Redis.from_url("redis://localhost:1/0", socket_connect_timeout=0.2)
    b = RedisCircuitBreaker(dead, {"p": BreakerPolicy(3, 30)})
    assert await b.allow("p")
    await b.record("p", U)  # does not raise
