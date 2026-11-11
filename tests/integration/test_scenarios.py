"""Acceptance scenarios (section 13). Every scenario ends with the ledger invariant."""

import asyncio

import pytest
from sqlalchemy import text

from cascade.application.payments import CreatePayment
from cascade.domain.money import Money
from cascade.domain.providers import ChargeRequest, ChargeResult
from mock_provider.app import Record
from tests.integration.conftest import HarnessFactory
from tests.integration.harness import Harness, make_config

pytestmark = pytest.mark.integration


def statuses(p: dict) -> list[tuple[str, str]]:
    return [(a["provider"], a["status"]) for a in p["attempts"]]


# --------------------------------------------------------------------------- M2


async def test_s1_approve_on_first_provider(env: Harness) -> None:
    r = await env.pay()
    assert r.status_code == 201
    p = r.json()
    assert p["status"] == "SUCCEEDED"
    assert statuses(p) == [("mock_a", "SUCCEEDED")]
    assert "tok_" not in r.text  # token never leaves the service
    await env.assert_ledger_invariant(p["id"])


async def test_s2_soft_decline_cascades(env: Harness) -> None:
    await env.script("mock_a", "soft_decline:do_not_honor")
    p = (await env.pay()).json()
    assert p["status"] == "SUCCEEDED"
    assert statuses(p) == [("mock_a", "DECLINED_SOFT"), ("mock_b", "SUCCEEDED")]
    assert p["attempts"][0]["reason"] == "do_not_honor"
    await env.assert_ledger_invariant(p["id"])


async def test_s3_hard_decline_stops(env: Harness) -> None:
    await env.script("mock_a", "hard_decline:insufficient_funds")
    r = await env.pay()
    assert r.status_code == 201
    p = r.json()
    assert p["status"] == "FAILED"
    assert p["failure_reason"] == "insufficient_funds"
    assert statuses(p) == [("mock_a", "DECLINED_HARD")]
    assert env.calls("mock_b") == []
    await env.assert_ledger_invariant(p["id"])


async def test_unmapped_code_is_hard(env: Harness) -> None:
    await env.script("mock_a", "soft_decline:weird_code_77")
    p = (await env.pay()).json()
    assert p["status"] == "FAILED"
    assert statuses(p) == [("mock_a", "DECLINED_HARD")]
    assert p["attempts"][0]["reason"] == "unmapped"
    assert env.calls("mock_b") == []


async def test_s8_concurrent_same_idempotency_key(env: Harness) -> None:
    rs = await asyncio.gather(*(env.pay(key="same-key") for _ in range(10)))
    assert all(r.status_code in (201, 202) for r in rs), [r.text for r in rs]
    ids = {r.json()["id"] for r in rs}
    assert len(ids) == 1
    pid = ids.pop()
    p = await env.assert_ledger_invariant(pid)
    assert p["status"] == "SUCCEEDED"
    assert len(p["attempts"]) == 1
    assert len(env.calls("mock_a")) + len(env.calls("mock_b")) == 1


async def test_idempotent_replay_and_conflict(env: Harness) -> None:
    first = await env.pay(key="k1", amount=1000)
    again = await env.pay(key="k1", amount=1000)
    assert again.status_code == 201
    assert again.json()["id"] == first.json()["id"]
    assert len(env.calls("mock_a")) == 1  # no new processing
    conflict = await env.pay(key="k1", amount=2000)
    assert conflict.status_code == 422


async def test_request_validation(env: Harness) -> None:
    bad = [
        {"amount_minor": 10.5, "currency": "USD", "payment_method": {"token": "t"}},
        {"amount_minor": 0, "currency": "USD", "payment_method": {"token": "t"}},
        {"amount_minor": 100, "currency": "usd", "payment_method": {"token": "t"}},
        {"amount_minor": 100, "currency": "USD", "payment_method": {"token": "t", "pan": "4242"}},
    ]
    for body in bad:
        r = await env.api.post("/payments", json=body, headers={"Idempotency-Key": "x"})
        assert r.status_code == 422, body
    r = await env.api.post(
        "/payments", json={"amount_minor": 1, "currency": "USD", "payment_method": {"token": "t"}}
    )
    assert r.status_code == 400  # missing Idempotency-Key


async def test_s10_route_exhausted(env: Harness) -> None:
    await env.script("mock_a", "soft_decline:do_not_honor")
    await env.script("mock_b", "soft_decline:generic_decline")
    p = (await env.pay()).json()
    assert p["status"] == "FAILED"
    assert p["failure_reason"] == "generic_decline"
    assert statuses(p) == [("mock_a", "DECLINED_SOFT"), ("mock_b", "DECLINED_SOFT")]
    await env.assert_ledger_invariant(p["id"])


async def test_tombstoned_reference_is_failed_not_charged(env: Harness) -> None:
    adapter = env.svc.adapters["mock_a"]
    await adapter.cancel("tombstoned-ref")
    res = await adapter.charge(ChargeRequest("tombstoned-ref", 100, "USD", "tok"))
    assert res.outcome.value == "FAILED_NOT_CHARGED"


# --------------------------------------------------------------------------- M3


async def test_s5_timeout_charged_resolved_by_lookup(env: Harness) -> None:
    await env.script("mock_a", "timeout_charged")
    r = await env.pay()
    assert r.status_code == 201
    p = r.json()
    assert p["status"] == "SUCCEEDED"
    assert statuses(p) == [("mock_a", "SUCCEEDED")]
    assert env.calls("mock_b") == []
    await env.assert_ledger_invariant(p["id"])


async def test_s6_late_charge_cancelled_then_cascade(env: Harness) -> None:
    await env.script("mock_a", "late_charge:5")
    p = (await env.pay()).json()
    assert p["status"] == "SUCCEEDED"
    assert statuses(p) == [("mock_a", "VOIDED"), ("mock_b", "SUCCEEDED")]
    env.clock.advance(6)
    ledger_a = await env.ledger("mock_a")
    assert all(e["state"] != "charged" for e in ledger_a.values())
    await env.assert_ledger_invariant(p["id"])


async def test_s7_unresolvable_unknown_parks_then_worker_resolves(env: Harness) -> None:
    await env.script("mock_a", "timeout_charged")
    await env.flags("mock_a", lookup_down=True, cancel_down=True)
    r = await env.pay()
    assert r.status_code == 202
    p = r.json()
    assert p["status"] == "PENDING_RECONCILIATION"
    assert statuses(p) == [("mock_a", "UNKNOWN")]
    assert env.calls("mock_b") == []
    assert [str(x) for x in env.queue.enqueued] == [p["id"]]

    # Outage still on: reconciliation keeps it parked, still no second provider.
    await env.svc.engine.sweep()
    assert (await env.get(p["id"]))["status"] == "PENDING_RECONCILIATION"
    assert env.calls("mock_b") == []

    await env.flags("mock_a", lookup_down=False, cancel_down=False)
    await env.svc.engine.sweep()  # what the worker does periodically
    p = await env.assert_ledger_invariant(p["id"])
    assert p["status"] == "SUCCEEDED"
    assert statuses(p) == [("mock_a", "SUCCEEDED")]
    assert env.calls("mock_b") == []


async def test_reconcile_declined_resumes_cascade(env: Harness) -> None:
    await env.script("mock_a", "timeout")  # never charged, lookup will say NOT_FOUND
    await env.flags("mock_a", cancel_down=True)
    p = (await env.pay()).json()
    assert p["status"] == "PENDING_RECONCILIATION"
    # Provider-side the reference turns out declined (it reports it on the next lookup).
    ref = p["attempts"][0]["id"]
    env.states["mock_a"].records[ref] = Record(ref, 1000, "USD", "declined", "t", "do_not_honor")
    await env.flags("mock_a", cancel_down=False)
    await env.svc.engine.sweep()
    p = await env.assert_ledger_invariant(p["id"])
    assert statuses(p) == [("mock_a", "DECLINED_SOFT"), ("mock_b", "SUCCEEDED")]


async def test_unknown_max_age_fails_for_review(make_env: HarnessFactory) -> None:
    env = await make_env(make_config(cascade={"unknown_max_age_s": 60}))
    await env.script("mock_a", "timeout")
    await env.flags("mock_a", lookup_down=True, cancel_down=True)
    p = (await env.pay()).json()
    assert p["status"] == "PENDING_RECONCILIATION"
    env.clock.advance(61)
    await env.svc.engine.sweep()
    p = await env.assert_ledger_invariant(p["id"])
    assert p["status"] == "FAILED"
    assert p["failure_reason"] == "unknown_outcome"
    assert p["needs_review"] is True
    assert env.calls("mock_b") == []


class Crash(Exception):
    pass


class CrashAfterCharge:
    """The provider charges; the process dies before the result is saved."""

    def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
        self.inner = inner
        self.name = inner.name
        self.cancel_blocks_late_charge = inner.cancel_blocks_late_charge

    async def charge(self, req: ChargeRequest) -> ChargeResult:
        await self.inner.charge(req)
        raise Crash

    async def lookup(self, reference: str):  # type: ignore[no-untyped-def]
        return await self.inner.lookup(reference)

    async def cancel(self, reference: str):  # type: ignore[no-untyped-def]
        return await self.inner.cancel(reference)


async def test_s11_crash_after_write_ahead_recovered_by_sweep(env: Harness) -> None:
    real = dict(env.svc.engine.adapters)
    env.svc.engine.adapters = {**real, "mock_a": CrashAfterCharge(real["mock_a"])}
    cmd = CreatePayment("crash-key", Money(1000, "USD"), "tok", None, None)
    with pytest.raises(Crash):
        await env.svc.payments.create(cmd)
    env.svc.engine.adapters = real

    async with env.svc.db.connect() as conn:
        pid = (
            await conn.execute(text("SELECT id FROM payments WHERE idempotency_key = 'crash-key'"))
        ).scalar_one()
    p, attempts = await env.svc.payments.get(pid)
    assert p.status.value == "PROCESSING"
    assert [a.status.value for a in attempts] == ["STARTED"]  # write-ahead row survived

    assert await env.svc.engine.sweep() == 0  # lease still valid: nobody touches it
    await env.expire_lease(str(p.id))
    assert await env.svc.engine.sweep() == 1
    out = await env.assert_ledger_invariant(str(p.id))
    assert out["status"] == "SUCCEEDED"
    assert statuses(out) == [("mock_a", "SUCCEEDED")]
    assert len(env.calls("mock_a")) == 1
    assert env.calls("mock_b") == []


# --------------------------------------------------------------------------- M4


async def test_s4_breaker_opens_and_probes_after_cooldown(env: Harness) -> None:
    await env.script("mock_a", "error_500", "error_500", "error_500")
    for _ in range(3):
        p = (await env.pay()).json()
        assert p["status"] == "SUCCEEDED"
        assert statuses(p) == [("mock_a", "VOIDED"), ("mock_b", "SUCCEEDED")]
        await env.assert_ledger_invariant(p["id"])

    providers = {x["name"]: x for x in (await env.api.get("/providers")).json()}
    assert providers["mock_a"]["breaker_state"] == "OPEN"

    calls_a = len(env.calls("mock_a"))
    for _ in range(2):
        p = (await env.pay()).json()
        assert p["route"] == ["mock_b"]
        assert statuses(p) == [("mock_b", "SUCCEEDED")]
    assert len(env.calls("mock_a")) == calls_a

    env.clock.advance(31)  # cooldown_s = 30
    p = (await env.pay()).json()
    assert p["route"] == ["mock_a", "mock_b"]  # the single half-open probe
    assert statuses(p) == [("mock_a", "SUCCEEDED")]
    providers = {x["name"]: x for x in (await env.api.get("/providers")).json()}
    assert providers["mock_a"]["breaker_state"] == "CLOSED"


async def test_breaker_ignores_declines(env: Harness) -> None:
    await env.script("mock_a", *["soft_decline"] * 5)
    for _ in range(5):
        await env.pay()
    providers = {x["name"]: x for x in (await env.api.get("/providers")).json()}
    assert providers["mock_a"]["breaker_state"] == "CLOSED"
    assert providers["mock_a"]["consecutive_failures"] == 0


async def test_s9_duplicate_webhook_and_late_success(env: Harness) -> None:
    # Duplicate webhook is a no-op.
    await env.flags("mock_a", webhooks="duplicate")
    p1 = (await env.pay()).json()
    assert p1["status"] == "SUCCEEDED"
    results = await env.deliver_webhooks("mock_a")
    assert [r["status"] for r in results] == ["duplicate", "duplicate"]  # already settled

    # Late success: A's cancel does not really block the late charge.
    await env.reset_mocks()
    await env.flags("mock_a", webhooks="duplicate", cancel_blocks_late_charge=False)
    await env.script("mock_a", "late_charge:5")
    p = (await env.pay()).json()
    assert p["status"] == "SUCCEEDED"
    assert statuses(p) == [("mock_a", "VOIDED"), ("mock_b", "SUCCEEDED")]

    env.clock.advance(6)  # A charges late, emits charge.succeeded (twice)
    assert await env.charged(p) == 2  # momentarily double-charged
    results = await env.deliver_webhooks("mock_a")
    assert [r["status"] for r in results] == ["late_success_refunded", "duplicate"]

    p = await env.assert_ledger_invariant(p["id"])
    assert statuses(p) == [("mock_a", "REFUNDED_DUPLICATE"), ("mock_b", "SUCCEEDED")]
    assert p["needs_review"] is False


async def test_webhook_resolves_pending_payment(env: Harness) -> None:
    await env.script("mock_a", "timeout_charged")
    await env.flags("mock_a", lookup_down=True, cancel_down=True, webhooks="once")
    p = (await env.pay()).json()
    assert p["status"] == "PENDING_RECONCILIATION"
    assert [r["status"] for r in await env.deliver_webhooks("mock_a")] == ["applied"]
    p = await env.assert_ledger_invariant(p["id"])
    assert p["status"] == "SUCCEEDED"


async def test_webhook_signature_required(env: Harness) -> None:
    body = b'{"event_id": "e1", "reference": "x", "type": "charge.succeeded"}'
    r = await env.api.post("/webhooks/mock_a", content=body, headers={"X-Signature": "bad"})
    assert r.status_code == 401
    from tests.integration.harness import sign

    r = await env.api.post("/webhooks/mock_a", content=body, headers={"X-Signature": sign(body)})
    assert r.status_code == 200
    assert r.json()["status"] == "ignored_unknown_reference"
    r = await env.api.post("/webhooks/mock_a", content=body, headers={"X-Signature": sign(body)})
    assert r.json()["status"] == "duplicate"


async def test_s12_routing_rules(env: Harness) -> None:
    p = (await env.pay(currency="EUR", country="DE")).json()
    assert p["route"] == ["mock_b", "mock_a"]
    assert statuses(p) == [("mock_b", "SUCCEEDED")]

    p = (await env.pay(currency="EUR", country="IT")).json()
    assert p["route"] == ["mock_a", "mock_b"]  # falls through to default

    p = (await env.pay(amount=50_000)).json()
    assert p["route"] == ["mock_a"]

    p = (await env.pay(amount=49_999)).json()
    assert p["route"] == ["mock_a", "mock_b"]

    p = (await env.pay(currency="GBP")).json()
    assert sorted(p["route"]) == ["mock_a", "mock_b"]


async def test_no_route_when_all_breakers_open(make_env: HarnessFactory) -> None:
    env = await make_env(make_config(providers={"mock_a": {"failure_threshold": 1}}))
    await env.script("mock_a", "error_500")
    await env.pay(amount=50_000)  # route [mock_a] only; A fails, breaker opens
    r = await env.pay(amount=50_000)
    assert r.status_code == 201
    p = r.json()
    assert p["status"] == "FAILED"
    assert p["failure_reason"] == "no_route"
    assert p["attempts"] == []


# --------------------------------------------------------------------------- M6


async def test_s13_stats(env: Harness) -> None:
    await env.pay()  # S1
    await env.script("mock_a", "soft_decline")
    await env.pay()  # S2
    await env.script("mock_a", "hard_decline")
    await env.pay()  # S3
    await env.script("mock_a", "soft_decline")
    await env.script("mock_b", "soft_decline")
    await env.pay()  # S10

    s = (await env.api.get("/stats", params={"window_min": 60})).json()
    o = s["overall"]
    assert o["payments"] == 4
    assert o["succeeded"] == 2
    assert o["succeeded_first_attempt"] == 1
    assert o["cascade_saved"] == 1
    assert o["cascade_save_rate"] == 0.5
    a, b = s["providers"]["mock_a"], s["providers"]["mock_b"]
    assert (a["attempts"], a["approved"], a["soft_declines"], a["hard_declines"]) == (4, 1, 2, 1)
    assert (b["attempts"], b["approved"], b["soft_declines"], b["hard_declines"]) == (2, 1, 1, 0)
    assert a["approval_rate"] == 0.25
    assert a["p50_ms"] is not None and a["p95_ms"] >= a["p50_ms"]
