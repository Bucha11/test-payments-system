"""Wiring: builds every service from settings + config. One place, no framework magic."""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cascade.application.config import RoutingConfig
from cascade.application.engine import CascadeEngine
from cascade.application.payments import PaymentService, ProviderEvents
from cascade.application.ports import TaskQueue
from cascade.domain.providers import ProviderAdapter
from cascade.infrastructure.breaker import BreakerPolicy, RedisCircuitBreaker
from cascade.infrastructure.config_loader import load_routing_config
from cascade.infrastructure.db.repo import SqlPaymentRepository
from cascade.infrastructure.db.session import make_engine, make_sessionmaker
from cascade.infrastructure.providers.mock import MockProviderAdapter
from cascade.infrastructure.settings import Settings


@dataclass
class Services:
    settings: Settings
    config: RoutingConfig
    db: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    repo: SqlPaymentRepository
    adapters: Mapping[str, ProviderAdapter]
    breaker: RedisCircuitBreaker
    queue: TaskQueue
    engine: CascadeEngine
    payments: PaymentService
    events: ProviderEvents
    extra_closers: list[Callable[[], object]] = field(default_factory=list)

    async def aclose(self) -> None:
        for a in self.adapters.values():
            if isinstance(a, MockProviderAdapter):
                await a.aclose()
        await self.redis.aclose()
        await self.db.dispose()


class _NullQueue:
    async def enqueue_reconcile(self, payment_id: UUID) -> None:
        return None


def build_adapters(
    config: RoutingConfig, transports: Mapping[str, httpx.AsyncBaseTransport] | None = None
) -> dict[str, ProviderAdapter]:
    adapters: dict[str, ProviderAdapter] = {}
    for name, pc in config.providers.items():
        if not pc.enabled:
            continue
        if pc.type == "mock":
            assert pc.base_url is not None
            adapters[name] = MockProviderAdapter(
                name,
                pc.base_url,
                pc.timeout_s,
                cancel_blocks_late_charge=(
                    True if pc.cancel_blocks_late_charge is None else pc.cancel_blocks_late_charge
                ),
                transport=(transports or {}).get(name),
            )
        # type == "braintree": optional M7, not built (see NOTES.md).
    return adapters


def build_services(
    settings: Settings,
    *,
    config: RoutingConfig | None = None,
    queue: TaskQueue | None = None,
    transports: Mapping[str, httpx.AsyncBaseTransport] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    breaker_clock: Callable[[], float] = time.time,
) -> Services:
    config = config or load_routing_config(settings.routing_config)
    db = make_engine(settings.database_url)
    sm = make_sessionmaker(db)
    redis = Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)
    repo = SqlPaymentRepository(sm, lease_s=config.cascade.lease_s)
    adapters = build_adapters(config, transports)
    breaker = RedisCircuitBreaker(
        redis,
        {n: BreakerPolicy(p.failure_threshold, p.cooldown_s) for n, p in config.providers.items()},
        clock=breaker_clock,
    )
    queue = queue or _NullQueue()
    engine = CascadeEngine(
        config=config, repo=repo, adapters=adapters, breaker=breaker, queue=queue, clock=clock
    )
    return Services(
        settings=settings,
        config=config,
        db=db,
        sessionmaker=sm,
        redis=redis,
        repo=repo,
        adapters=adapters,
        breaker=breaker,
        queue=queue,
        engine=engine,
        payments=PaymentService(repo, engine),
        events=ProviderEvents(repo, adapters, queue),
    )
