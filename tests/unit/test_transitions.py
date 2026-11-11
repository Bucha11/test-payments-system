import itertools

import pytest

from cascade.domain.enums import AttemptStatus as A
from cascade.domain.enums import PaymentStatus as P
from cascade.domain.transitions import (
    ATTEMPT_MONEY_FREE,
    ATTEMPT_UNRESOLVED,
    PAYMENT_TERMINAL,
    IllegalTransition,
    TransitionKind,
    transition_attempt,
    transition_payment,
)

# The spec's diagrams, written out by hand independently of the implementation table.
LEGAL_PAYMENT = {
    (P.CREATED, P.PROCESSING),
    (P.CREATED, P.FAILED),  # no_route
    (P.PROCESSING, P.SUCCEEDED),
    (P.PROCESSING, P.FAILED),
    (P.PROCESSING, P.PENDING_RECONCILIATION),
    (P.PENDING_RECONCILIATION, P.PROCESSING),
    (P.PENDING_RECONCILIATION, P.SUCCEEDED),
    (P.PENDING_RECONCILIATION, P.FAILED),
}
LEGAL_ATTEMPT = {
    (A.STARTED, A.SUCCEEDED),
    (A.STARTED, A.DECLINED_SOFT),
    (A.STARTED, A.DECLINED_HARD),
    (A.STARTED, A.FAILED_NOT_CHARGED),
    (A.STARTED, A.UNKNOWN),
    (A.UNKNOWN, A.SUCCEEDED),
    (A.UNKNOWN, A.DECLINED_SOFT),
    (A.UNKNOWN, A.DECLINED_HARD),
    (A.UNKNOWN, A.VOIDED),
    (A.UNKNOWN, A.FAILED_NOT_CHARGED),
    (A.SUCCEEDED, A.REFUNDED_DUPLICATE),
    # late success (8.3): provider charged an attempt we had settled; we cancelled it
    (A.UNKNOWN, A.REFUNDED_DUPLICATE),
    (A.DECLINED_SOFT, A.REFUNDED_DUPLICATE),
    (A.DECLINED_HARD, A.REFUNDED_DUPLICATE),
    (A.FAILED_NOT_CHARGED, A.REFUNDED_DUPLICATE),
    (A.VOIDED, A.REFUNDED_DUPLICATE),
}


def _ids(pair: tuple[object, object]) -> str:
    return f"{pair[0]}->{pair[1]}"


@pytest.mark.parametrize("pair", sorted(LEGAL_PAYMENT), ids=_ids)
def test_payment_legal(pair: tuple[P, P]) -> None:
    t = transition_payment(*pair)
    assert t.kind is TransitionKind.APPLIED
    assert t.new_status is pair[1]
    assert t.require() is t


ILLEGAL_PAYMENT = [
    (a, b) for a, b in itertools.product(P, P) if a != b and (a, b) not in LEGAL_PAYMENT
]


@pytest.mark.parametrize("pair", ILLEGAL_PAYMENT, ids=_ids)
def test_payment_illegal(pair: tuple[P, P]) -> None:
    t = transition_payment(*pair)
    assert t.kind is TransitionKind.REJECTED
    assert t.new_status is pair[0]
    with pytest.raises(IllegalTransition):
        t.require()


@pytest.mark.parametrize("pair", sorted(LEGAL_ATTEMPT), ids=_ids)
def test_attempt_legal(pair: tuple[A, A]) -> None:
    t = transition_attempt(*pair)
    assert t.kind is TransitionKind.APPLIED
    assert t.new_status is pair[1]


ILLEGAL_ATTEMPT = [
    (a, b) for a, b in itertools.product(A, A) if a != b and (a, b) not in LEGAL_ATTEMPT
]


@pytest.mark.parametrize("pair", ILLEGAL_ATTEMPT, ids=_ids)
def test_attempt_illegal(pair: tuple[A, A]) -> None:
    t = transition_attempt(*pair)
    assert t.kind is TransitionKind.REJECTED
    assert t.new_status is pair[0]
    with pytest.raises(IllegalTransition):
        t.require()


@pytest.mark.parametrize("s", list(P))
def test_payment_same_state_is_duplicate_noop(s: P) -> None:
    t = transition_payment(s, s)
    assert t.kind is TransitionKind.DUPLICATE
    assert not t.applied
    assert t.new_status is s


@pytest.mark.parametrize("s", list(A))
def test_attempt_same_state_is_duplicate_noop(s: A) -> None:
    """A duplicate webhook / repeated lookup result must be a no-op."""
    t = transition_attempt(s, s)
    assert t.kind is TransitionKind.DUPLICATE
    assert t.new_status is s


def test_terminal_sets() -> None:
    assert {P.SUCCEEDED, P.FAILED} == PAYMENT_TERMINAL
    assert {A.STARTED, A.UNKNOWN} == ATTEMPT_UNRESOLVED
    # every status is exactly one of: unresolved, money-free, or the single success
    assert ATTEMPT_UNRESOLVED | ATTEMPT_MONEY_FREE | {A.SUCCEEDED} == set(A)
    assert not ATTEMPT_UNRESOLVED & ATTEMPT_MONEY_FREE


def test_refunded_duplicate_is_final_and_declines_never_become_succeeded() -> None:
    for s in A:
        assert not transition_attempt(A.REFUNDED_DUPLICATE, s).applied
    for s in (A.DECLINED_SOFT, A.DECLINED_HARD, A.FAILED_NOT_CHARGED, A.VOIDED):
        assert transition_attempt(s, A.SUCCEEDED).kind is TransitionKind.REJECTED


def test_out_of_order_event_cannot_regress_attempt() -> None:
    """E.g. webhook says SUCCEEDED, then a stale 'UNKNOWN' or a decline arrives."""
    assert transition_attempt(A.SUCCEEDED, A.UNKNOWN).kind is TransitionKind.REJECTED
    assert transition_attempt(A.SUCCEEDED, A.DECLINED_SOFT).kind is TransitionKind.REJECTED
    assert transition_attempt(A.UNKNOWN, A.STARTED).kind is TransitionKind.REJECTED


def test_terminal_payment_never_changes() -> None:
    for terminal in (P.SUCCEEDED, P.FAILED):
        for target in P:
            assert not transition_payment(terminal, target).applied
