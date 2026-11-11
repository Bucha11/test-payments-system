"""Test harness: the real engine + API, in-process mock providers, real Postgres/Redis."""

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text

from cascade.api.app import create_app
from cascade.application.config import RoutingConfig
from cascade.infrastructure.config_loader import load_routing_config
from cascade.infrastructure.container import Services, build_services
from cascade.infrastructure.settings import Settings
from mock_provider.app import MockState
from mock_provider.app import create_app as create_mock

WEBHOOK_SECRET = "test-webhook-secret"
PROVIDERS = ("mock_a", "mock_b")


class FakeClock:
    """Real time plus a manual offset: DB-side now() and leases keep working, while
    deadlines, unknown_max_age, breaker cooldowns and late charges can be fast-forwarded."""

    def __init__(self) -> None:
        self.offset = 0.0

    def now(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self.offset)

    def ts(self) -> float:
        return time.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


class RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def enqueue_reconcile(self, payment_id: uuid.UUID) -> None:
        self.enqueued.append(payment_id)


def make_config(
    *,
    timeout_s: float = 0.3,
    cascade: dict[str, Any] | None = None,
    providers: dict[str, dict[str, Any]] | None = None,
) -> RoutingConfig:
    base = load_routing_config("config/routing.yaml")
    c = base.cascade.model_copy(
        update={
            "lookup_backoff_s": 0,
            "reconcile_backoff_min_s": 0,
            "reconcile_backoff_max_s": 0,
            **(cascade or {}),
        }
    )
    provs = {}
    for name, pc in base.providers.items():
        upd: dict[str, Any] = {"timeout_s": timeout_s}
        upd.update((providers or {}).get(name, {}))
        provs[name] = pc.model_copy(update=upd)
    return base.model_copy(update={"cascade": c, "providers": provs})


@dataclass
class Harness:
    svc: Services
    api: httpx.AsyncClient
    mocks: dict[str, httpx.AsyncClient]
    states: dict[str, MockState]
    clock: FakeClock
    queue: RecordingQueue
    _keys: int = field(default=0)

    # ------------------------------------------------------------- API
    async def pay(
        self,
        *,
        key: str | None = None,
        amount: int = 1000,
        currency: str = "USD",
        country: str | None = None,
        bin_: str | None = None,
        token: str = "tok_visa_4242",
    ) -> httpx.Response:
        if key is None:
            self._keys += 1
            key = f"key-{uuid.uuid4()}"
        pm: dict[str, Any] = {"token": token}
        if country:
            pm["country"] = country
        if bin_:
            pm["bin"] = bin_
        return await self.api.post(
            "/payments",
            json={"amount_minor": amount, "currency": currency, "payment_method": pm},
            headers={"Idempotency-Key": key},
        )

    async def get(self, payment_id: str) -> dict[str, Any]:
        r = await self.api.get(f"/payments/{payment_id}")
        assert r.status_code in (201, 202), r.text
        data: dict[str, Any] = r.json()
        return data

    # ------------------------------------------------------------- mocks
    async def script(self, name: str, *steps: str) -> None:
        r = await self.mocks[name].post("/_admin/script", json={"steps": list(steps)})
        assert r.status_code == 200, r.text

    async def flags(self, name: str, **flags: Any) -> None:
        r = await self.mocks[name].post("/_admin/flags", json=flags)
        assert r.status_code == 200, r.text

    async def reset_mocks(self) -> None:
        for c in self.mocks.values():
            await c.post("/_admin/reset")

    async def ledger(self, name: str) -> dict[str, Any]:
        data: dict[str, Any] = (await self.mocks[name].get("/_admin/ledger")).json()
        return data

    def calls(self, name: str) -> list[dict[str, Any]]:
        return self.states[name].calls

    async def charged(self, payment: dict[str, Any]) -> int:
        refs = {a["id"] for a in payment["attempts"]}
        n = 0
        for name in self.mocks:
            for ref, e in (await self.ledger(name)).items():
                if ref in refs and e["state"] == "charged":
                    n += 1
        return n

    async def assert_ledger_invariant(self, payment_id: str) -> dict[str, Any]:
        """Charged (non-voided) entries for the payment: 1 if SUCCEEDED, otherwise 0."""
        p = await self.get(payment_id)
        expected = 1 if p["status"] == "SUCCEEDED" else 0
        got = await self.charged(p)
        assert got == expected, f"ledger invariant violated: {got} charges, payment {p}"
        return p

    async def deliver_webhooks(self, name: str) -> list[dict[str, Any]]:
        """Deliver queued mock webhooks to the API (in order, each once)."""
        out = []
        for ev in self.states[name].outbox:
            if ev["delivered"]:
                continue
            r = await self.api.post(
                f"/webhooks/{name}",
                content=ev["body"],
                headers={"X-Signature": ev["signature"], "Content-Type": "application/json"},
            )
            assert r.status_code == 200, r.text
            ev["delivered"] = True
            out.append(r.json())
        return out

    async def expire_lease(self, payment_id: str) -> None:
        async with self.svc.db.begin() as conn:
            await conn.execute(
                text("UPDATE payments SET lease_until = now() - interval '1 second' WHERE id=:id"),
                {"id": payment_id},
            )

    async def close(self) -> None:
        await self.api.aclose()
        for c in self.mocks.values():
            await c.aclose()
        await self.svc.aclose()


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def make_harness(
    database_url: str, redis_url: str, config: RoutingConfig | None = None
) -> Harness:
    clock = FakeClock()
    queue = RecordingQueue()
    settings = Settings(
        database_url=database_url, redis_url=redis_url, webhook_secret=WEBHOOK_SECRET
    )
    mock_apps = {n: create_mock(n, clock=clock.ts, secret=WEBHOOK_SECRET) for n in PROVIDERS}
    transports = {n: httpx.ASGITransport(app=a) for n, a in mock_apps.items()}
    svc = build_services(
        settings,
        config=config or make_config(),
        queue=queue,
        transports=transports,
        clock=clock.now,
        breaker_clock=clock.ts,
    )
    async with svc.db.begin() as conn:
        await conn.execute(text("TRUNCATE payments, attempts, webhook_events CASCADE"))
    await svc.redis.flushdb()
    api = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(svc)), base_url="http://api"
    )
    mocks = {
        n: httpx.AsyncClient(transport=transports[n], base_url=f"http://{n}") for n in PROVIDERS
    }
    states = {n: a.state.mock for n, a in mock_apps.items()}
    return Harness(svc, api, mocks, states, clock, queue)


def dumps(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True).encode()
