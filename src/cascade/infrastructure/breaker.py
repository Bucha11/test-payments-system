"""Per-provider circuit breaker in Redis (8.5).

Stored per provider: hash `breaker:<name>` {failures, opened_at} and a probe key.
State is derived, not stored:
    no opened_at                       -> CLOSED
    now <  opened_at + cooldown        -> OPEN
    now >= opened_at + cooldown        -> HALF_OPEN (exactly one probe gets through:
                                          SET probe NX; the key expires after cooldown so a
                                          probe that never reports does not wedge the breaker)

Only FAILED_NOT_CHARGED and UNKNOWN count as failures. A decline is a healthy provider
saying "no", so it counts as success. If Redis is down the breaker fails open (allows).
"""

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from cascade.application.ports import BreakerSnapshot
from cascade.domain.enums import BreakerState, ChargeOutcome

log = logging.getLogger(__name__)

FAILURE_OUTCOMES = frozenset({ChargeOutcome.FAILED_NOT_CHARGED, ChargeOutcome.UNKNOWN})

# KEYS[1]=hash KEYS[2]=probe ARGV[1]=now ARGV[2]=cooldown ARGV[3]=threshold
_RECORD_FAILURE = """
local opened = redis.call('HGET', KEYS[1], 'opened_at')
if opened then
  if tonumber(ARGV[1]) >= tonumber(opened) + tonumber(ARGV[2]) then
    redis.call('HSET', KEYS[1], 'opened_at', ARGV[1])
    redis.call('DEL', KEYS[2])
  end
  return 0
end
local f = redis.call('HINCRBY', KEYS[1], 'failures', 1)
if f >= tonumber(ARGV[3]) then
  redis.call('HSET', KEYS[1], 'opened_at', ARGV[1])
end
return f
"""


@dataclass(frozen=True, slots=True)
class BreakerPolicy:
    failure_threshold: int
    cooldown_s: float


class RedisCircuitBreaker:
    def __init__(
        self,
        redis: Redis,
        policies: Mapping[str, BreakerPolicy],
        clock: Callable[[], float] = time.time,
        prefix: str = "breaker",
    ) -> None:
        self.r = redis
        self.policies = policies
        self.clock = clock
        self.prefix = prefix
        self._record_failure = redis.register_script(_RECORD_FAILURE)

    def _keys(self, provider: str) -> tuple[str, str]:
        return f"{self.prefix}:{provider}", f"{self.prefix}:{provider}:probe"

    async def snapshot(self, provider: str) -> BreakerSnapshot:
        h, _ = self._keys(provider)
        raw = await self.r.hgetall(h)
        failures = int(raw.get(b"failures", 0))
        opened = raw.get(b"opened_at")
        if opened is None:
            return BreakerSnapshot(BreakerState.CLOSED, failures, None)
        until = float(opened) + self.policies[provider].cooldown_s - self.clock()
        if until > 0:
            return BreakerSnapshot(BreakerState.OPEN, failures, until)
        return BreakerSnapshot(BreakerState.HALF_OPEN, failures, 0.0)

    async def allow(self, provider: str) -> bool:
        try:
            snap = await self.snapshot(provider)
            if snap.state is BreakerState.CLOSED:
                return True
            if snap.state is BreakerState.OPEN:
                return False
            _, probe = self._keys(provider)
            ttl = max(1, int(self.policies[provider].cooldown_s))
            got = await self.r.set(probe, "1", nx=True, ex=ttl)
            return bool(got)
        except RedisError:
            log.exception("breaker unavailable; allowing", extra={"provider": provider})
            return True

    async def record(self, provider: str, outcome: ChargeOutcome) -> None:
        h, probe = self._keys(provider)
        pol = self.policies[provider]
        try:
            if outcome in FAILURE_OUTCOMES:
                await self._record_failure(
                    keys=[h, probe], args=[self.clock(), pol.cooldown_s, pol.failure_threshold]
                )
            else:
                await self.r.delete(h, probe)  # success or healthy decline closes it
        except RedisError:
            log.exception("breaker unavailable; outcome not recorded", extra={"provider": provider})
