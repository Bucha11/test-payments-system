"""Mapping of provider answers to attempt statuses (pure functions)."""

from cascade.domain.enums import AttemptStatus, ChargeOutcome, LookupStatus
from cascade.domain.taxonomy import DeclineClass, NormalizedReason, classify

_CHARGE_TO_ATTEMPT = {
    ChargeOutcome.APPROVED: AttemptStatus.SUCCEEDED,
    ChargeOutcome.SOFT_DECLINE: AttemptStatus.DECLINED_SOFT,
    ChargeOutcome.HARD_DECLINE: AttemptStatus.DECLINED_HARD,
    ChargeOutcome.FAILED_NOT_CHARGED: AttemptStatus.FAILED_NOT_CHARGED,
    ChargeOutcome.UNKNOWN: AttemptStatus.UNKNOWN,
}


def attempt_status_for_charge(outcome: ChargeOutcome) -> AttemptStatus:
    return _CHARGE_TO_ATTEMPT[outcome]


def attempt_status_for_decline(reason: NormalizedReason) -> AttemptStatus:
    if classify(reason) is DeclineClass.SOFT:
        return AttemptStatus.DECLINED_SOFT
    return AttemptStatus.DECLINED_HARD


def attempt_status_for_lookup(
    status: LookupStatus, reason: NormalizedReason | None
) -> AttemptStatus | None:
    """None = lookup did not resolve the attempt (NOT_FOUND / UNKNOWN)."""
    if status is LookupStatus.APPROVED:
        return AttemptStatus.SUCCEEDED
    if status is LookupStatus.DECLINED:
        assert reason is not None
        return attempt_status_for_decline(reason)
    return None
