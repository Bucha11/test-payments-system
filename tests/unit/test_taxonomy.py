import logging

import pytest

from cascade.domain.taxonomy import (
    DECLINE_CLASS,
    DeclineClass,
    NormalizedReason,
    classify,
    is_cascadable,
    map_raw_code,
)

R = NormalizedReason

SOFT = {
    R.GENERIC_DECLINE,
    R.DO_NOT_HONOR,
    R.PROVIDER_UNAVAILABLE,
    R.ISSUER_UNAVAILABLE,
    R.PROVIDER_RISK_REJECT,
    R.UNSUPPORTED_CARD_OR_CURRENCY,
    R.RATE_LIMITED,
}
HARD = {
    R.INSUFFICIENT_FUNDS,
    R.EXPIRED_CARD,
    R.INVALID_CARD,
    R.INVALID_CVV,
    R.LOST_OR_STOLEN,
    R.PICKUP_CARD,
    R.CLOSED_ACCOUNT,
    R.AUTHENTICATION_FAILED,
    R.UNMAPPED,
}


def test_every_reason_is_classified_exactly_once() -> None:
    assert set(DECLINE_CLASS) == set(R)
    assert set(R) == SOFT | HARD
    assert not SOFT & HARD


@pytest.mark.parametrize("reason", sorted(SOFT))
def test_soft(reason: R) -> None:
    assert classify(reason) is DeclineClass.SOFT
    assert is_cascadable(reason)


@pytest.mark.parametrize("reason", sorted(HARD))
def test_hard(reason: R) -> None:
    assert classify(reason) is DeclineClass.HARD
    assert not is_cascadable(reason)


TABLE = {"05": R.DO_NOT_HONOR, "51": R.INSUFFICIENT_FUNDS}


def test_map_known_code() -> None:
    assert map_raw_code("p", TABLE, "05") is R.DO_NOT_HONOR
    assert map_raw_code("p", TABLE, "51") is R.INSUFFICIENT_FUNDS


@pytest.mark.parametrize("raw", ["99", "", None])
def test_unmapped_code_is_hard_and_logged(
    raw: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        reason = map_raw_code("p", TABLE, raw)
    assert reason is R.UNMAPPED
    assert classify(reason) is DeclineClass.HARD
    assert any("unmapped" in r.getMessage() for r in caplog.records)
