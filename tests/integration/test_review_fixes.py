"""Regression tests for two gaps found in review.

1. An APPROVED charge response that cannot be recorded (the attempt was already moved by
   a decline webhook) must not let the cascade charge the next provider.
2. A webhook that was received but not applied (crash in between) must be applied on
   redelivery, not dropped as a duplicate.
"""

import json
from typing import Any

import pytest

from cascade.application.payments import ProviderEvent
from cascade.domain.providers import ChargeRequest, ChargeResult
from cascade.domain.taxonomy import NormalizedReason
from tests.integration.harness import Harness, sign

pytestmark = pytest.mark.integration


def statuses(p: dict[str, Any]) -> list[tuple[str, str]]:
    return [(a["provider"], a["status"]) for a in p["attempts"]]


class DeclineWebhookRacesApproval:
    """Wraps mock_a: a decline webhook for the attempt lands while the charge is in
    flight, then the provider answers APPROVED (a provider contradicting itself)."""

    def __init__(self, env: Harness) -> None:
        self.env = env
        self.inner = env.svc.adapters["mock_a"]
        self.name = self.inner.name
        self.cancel_blocks_late_charge = self.inner.cancel_blocks_late_charge

    async def charge(self, req: ChargeRequest) -> ChargeResult:
        await self.env.svc.events.apply(
            ProviderEvent("mock_a", req.reference, False, NormalizedReason.DO_NOT_HONOR)
        )
        return await self.inner.charge(req)

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)


async def test_unrecorded_approval_is_cancelled_before_next_provider(env: Harness) -> None:
    env.svc.engine.adapters["mock_a"] = DeclineWebhookRacesApproval(env)  # type: ignore[index]
    p = (await env.pay()).json()
    assert statuses(p) == [("mock_a", "REFUNDED_DUPLICATE"), ("mock_b", "SUCCEEDED")]
    p = await env.assert_ledger_invariant(p["id"])  # A's charge was voided: one charge
    assert p["status"] == "SUCCEEDED"


async def test_unrecorded_approval_that_cannot_be_cancelled_stops(env: Harness) -> None:
    env.svc.engine.adapters["mock_a"] = DeclineWebhookRacesApproval(env)  # type: ignore[index]
    await env.flags("mock_a", cancel_down=True)
    p = (await env.pay()).json()
    assert p["status"] == "FAILED"
    assert p["failure_reason"] == "unrecorded_charge"
    assert p["needs_review"] is True
    assert env.calls("mock_b") == []  # no second charge


async def test_webhook_received_but_not_applied_is_applied_on_redelivery(env: Harness) -> None:
    # Park a payment with an UNKNOWN attempt (A hangs; lookup and cancel are down).
    await env.script("mock_a", "timeout")
    await env.flags("mock_a", lookup_down=True, cancel_down=True)
    p = (await env.pay()).json()
    assert statuses(p) == [("mock_a", "UNKNOWN")]
    ref = p["attempts"][0]["id"]

    payload = {"event_id": "e-crash", "type": "charge.declined", "reference": ref}
    payload["code"] = "do_not_honor"
    # First delivery: received, then the process died before applying it.
    assert await env.svc.repo.claim_webhook("mock_a", "e-crash", payload)

    body = json.dumps(payload).encode()
    headers = {"X-Signature": sign(body), "Content-Type": "application/json"}
    r = await env.api.post("/webhooks/mock_a", content=body, headers=headers)
    assert r.json()["status"] == "applied"
    assert statuses(await env.get(p["id"])) == [("mock_a", "DECLINED_SOFT")]

    r = await env.api.post("/webhooks/mock_a", content=body, headers=headers)
    assert r.json()["status"] == "duplicate"
