import pytest

from cascade.domain.enums import (
    AttemptStatus,
    CancelStatus,
    ChargeOutcome,
    LookupStatus,
)
from cascade.domain.money import Money
from cascade.domain.outcomes import (
    attempt_status_for_charge,
    attempt_status_for_lookup,
)
from cascade.domain.providers import (
    CancelResult,
    ChargeRequest,
    ChargeResult,
    LookupResult,
    ProviderAdapter,
)
from cascade.domain.taxonomy import NormalizedReason as R


def test_declined_derives_class_from_taxonomy() -> None:
    assert ChargeResult.declined(R.DO_NOT_HONOR).outcome is ChargeOutcome.SOFT_DECLINE
    assert ChargeResult.declined(R.INSUFFICIENT_FUNDS).outcome is ChargeOutcome.HARD_DECLINE
    assert ChargeResult.declined(R.UNMAPPED).outcome is ChargeOutcome.HARD_DECLINE


def test_adapter_cannot_mislabel_decline_class() -> None:
    with pytest.raises(ValueError):
        ChargeResult(ChargeOutcome.SOFT_DECLINE, R.INSUFFICIENT_FUNDS)
    with pytest.raises(ValueError):
        ChargeResult(ChargeOutcome.HARD_DECLINE, R.RATE_LIMITED)
    with pytest.raises(ValueError):
        ChargeResult(ChargeOutcome.SOFT_DECLINE)


def test_lookup_declined_needs_reason() -> None:
    with pytest.raises(ValueError):
        LookupResult(LookupStatus.DECLINED)


def test_pm_token_not_in_repr() -> None:
    req = ChargeRequest("ref", 100, "EUR", "tok_secret_123")
    assert "tok_secret_123" not in repr(req)


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (ChargeOutcome.APPROVED, AttemptStatus.SUCCEEDED),
        (ChargeOutcome.SOFT_DECLINE, AttemptStatus.DECLINED_SOFT),
        (ChargeOutcome.HARD_DECLINE, AttemptStatus.DECLINED_HARD),
        (ChargeOutcome.FAILED_NOT_CHARGED, AttemptStatus.FAILED_NOT_CHARGED),
        (ChargeOutcome.UNKNOWN, AttemptStatus.UNKNOWN),
    ],
)
def test_charge_outcome_to_attempt(outcome: ChargeOutcome, status: AttemptStatus) -> None:
    assert attempt_status_for_charge(outcome) is status


def test_lookup_to_attempt() -> None:
    assert attempt_status_for_lookup(LookupStatus.APPROVED, None) is AttemptStatus.SUCCEEDED
    assert (
        attempt_status_for_lookup(LookupStatus.DECLINED, R.GENERIC_DECLINE)
        is AttemptStatus.DECLINED_SOFT
    )
    assert (
        attempt_status_for_lookup(LookupStatus.DECLINED, R.EXPIRED_CARD)
        is AttemptStatus.DECLINED_HARD
    )
    assert attempt_status_for_lookup(LookupStatus.NOT_FOUND, None) is None
    assert attempt_status_for_lookup(LookupStatus.UNKNOWN, None) is None


class _Fake:
    name = "fake"
    cancel_blocks_late_charge = True

    async def charge(self, req: ChargeRequest) -> ChargeResult:
        return ChargeResult(ChargeOutcome.APPROVED, provider_txn_id="t1")

    async def lookup(self, reference: str) -> LookupResult:
        return LookupResult(LookupStatus.NOT_FOUND)

    async def cancel(self, reference: str) -> CancelResult:
        return CancelResult(CancelStatus.CANCELLED)


async def test_fake_satisfies_protocol() -> None:
    adapter: ProviderAdapter = _Fake()
    res = await adapter.charge(ChargeRequest("r", 1, "EUR", "t"))
    assert res.outcome is ChargeOutcome.APPROVED


@pytest.mark.parametrize(
    ("amount", "currency"),
    [(0, "EUR"), (-1, "EUR"), (1.5, "EUR"), (True, "EUR"), (100, "eur"), (100, "EURO")],
)
def test_money_rejects_bad_values(amount: object, currency: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        Money(amount, currency)  # type: ignore[arg-type]


def test_money_ok() -> None:
    assert Money(1999, "EUR").amount_minor == 1999
