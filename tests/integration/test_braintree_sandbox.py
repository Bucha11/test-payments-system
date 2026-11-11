"""Against the real Braintree sandbox. Skipped unless BRAINTREE_* credentials are set.

Sandbox test amounts (https://developer.paypal.com/braintree/docs/reference/general/
testing/python#test-amounts): 2000.00-2999.99 are processor-declined with that code,
3000.00 fails with "processor network unavailable".
"""

import uuid

import pytest

from cascade.domain.enums import CancelStatus, ChargeOutcome, LookupStatus
from cascade.domain.providers import ChargeRequest
from cascade.infrastructure.providers.braintree import BraintreeAdapter
from cascade.infrastructure.settings import Settings
from tests.integration.harness import Harness, make_config

SETTINGS = Settings()

pytestmark = pytest.mark.skipif(
    not SETTINGS.braintree_configured, reason="BRAINTREE_* credentials not set"
)


@pytest.fixture
def bt() -> BraintreeAdapter:
    return BraintreeAdapter.create(
        "braintree",
        merchant_id=SETTINGS.braintree_merchant_id,
        public_key=SETTINGS.braintree_public_key,
        private_key=SETTINGS.braintree_private_key,
        environment=SETTINGS.braintree_environment,
        timeout_s=15,
        currencies=["USD"],
    )


def req(amount_minor: int, token: str = "tok_any") -> ChargeRequest:
    return ChargeRequest(str(uuid.uuid4()), amount_minor, "USD", token)


async def test_approve_lookup_cancel(bt: BraintreeAdapter) -> None:
    r = req(1234)
    charged = await bt.charge(r)
    assert charged.outcome is ChargeOutcome.APPROVED
    assert charged.provider_txn_id

    found = await bt.lookup(r.reference)
    assert found.status is LookupStatus.APPROVED
    assert found.provider_txn_id == charged.provider_txn_id

    assert (await bt.cancel(r.reference)).status is CancelStatus.CANCELLED  # void
    assert (await bt.lookup(r.reference)).status is LookupStatus.VOIDED
    assert (await bt.cancel(r.reference)).status is CancelStatus.CANCELLED  # idempotent


async def test_soft_and_hard_declines(bt: BraintreeAdapter) -> None:
    soft = await bt.charge(req(200_000))  # 2000.00 -> 2000 Do Not Honor
    assert soft.outcome is ChargeOutcome.SOFT_DECLINE
    assert soft.raw_code == "2000"

    hard = await bt.charge(req(200_100))  # 2001.00 -> 2001 Insufficient Funds
    assert hard.outcome is ChargeOutcome.HARD_DECLINE

    down = await bt.charge(req(300_000))  # 3000.00 -> processor network unavailable
    assert down.outcome is ChargeOutcome.SOFT_DECLINE


async def test_lookup_of_unknown_reference(bt: BraintreeAdapter) -> None:
    assert (await bt.lookup(str(uuid.uuid4()))).status is LookupStatus.NOT_FOUND


# ------------------------------------------------------------------------- end to end


def _sandbox_config():  # type: ignore[no-untyped-def]
    # The harness shortens provider timeouts for the mocks; the sandbox needs real ones.
    return make_config(providers={"braintree": {"timeout_s": 5}})


async def test_e2e_braintree_approves(make_env) -> None:  # type: ignore[no-untyped-def]
    env: Harness = await make_env(_sandbox_config())
    p = (await env.pay(currency="USD", country="US", amount=4321)).json()
    assert p["route"] == ["braintree", "mock_a"]
    assert [(a["provider"], a["status"]) for a in p["attempts"]] == [("braintree", "SUCCEEDED")]
    assert p["status"] == "SUCCEEDED"
    assert env.calls("mock_a") == []


async def test_e2e_braintree_soft_decline_cascades_to_mock(make_env) -> None:  # type: ignore[no-untyped-def]
    env: Harness = await make_env(_sandbox_config())
    p = (await env.pay(currency="USD", country="US", amount=200_000)).json()
    assert [(a["provider"], a["status"]) for a in p["attempts"]] == [
        ("braintree", "DECLINED_SOFT"),
        ("mock_a", "SUCCEEDED"),
    ]
    assert p["status"] == "SUCCEEDED"
    # Exactly one charge overall: Braintree holds nothing for its attempt, mock_a one.
    bt = env.svc.adapters["braintree"]
    assert (await bt.lookup(p["attempts"][0]["id"])).status is LookupStatus.DECLINED
    assert await env.charged(p) == 1


async def test_e2e_braintree_hard_decline_stops(make_env) -> None:  # type: ignore[no-untyped-def]
    env: Harness = await make_env(_sandbox_config())
    p = (await env.pay(currency="USD", country="US", amount=200_100)).json()
    assert p["status"] == "FAILED"
    assert p["failure_reason"] == "insufficient_funds"
    assert env.calls("mock_a") == []
