"""State machines for payments and attempts.

Every status change goes through `transition_payment` / `transition_attempt`.
The engine, reconciliation, webhooks and polling all use the same functions, so:

* moving to the state you are already in is a DUPLICATE (no-op, not an error);
* a move the graph does not allow is REJECTED (e.g. a stale/out-of-order event
  trying to move a terminal attempt). Webhook/polling code treats REJECTED as a
  no-op and logs it; engine code calls `.require()` because for the engine an
  illegal move is a bug.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from cascade.domain.enums import AttemptStatus, PaymentStatus

P = PaymentStatus
A = AttemptStatus

PAYMENT_TRANSITIONS: Mapping[PaymentStatus, frozenset[PaymentStatus]] = {
    # CREATED -> FAILED: route is empty (`no_route`) before processing ever starts.
    P.CREATED: frozenset({P.PROCESSING, P.FAILED}),
    P.PROCESSING: frozenset({P.SUCCEEDED, P.FAILED, P.PENDING_RECONCILIATION}),
    P.PENDING_RECONCILIATION: frozenset({P.PROCESSING, P.SUCCEEDED, P.FAILED}),
    P.SUCCEEDED: frozenset(),
    P.FAILED: frozenset(),
}

ATTEMPT_TRANSITIONS: Mapping[AttemptStatus, frozenset[AttemptStatus]] = {
    A.STARTED: frozenset(
        {A.SUCCEEDED, A.DECLINED_SOFT, A.DECLINED_HARD, A.FAILED_NOT_CHARGED, A.UNKNOWN}
    ),
    A.UNKNOWN: frozenset(
        {
            A.SUCCEEDED,
            A.DECLINED_SOFT,
            A.DECLINED_HARD,
            A.VOIDED,
            A.FAILED_NOT_CHARGED,
            A.REFUNDED_DUPLICATE,
        }
    ),
    # -> REFUNDED_DUPLICATE only via late-success handling: the provider reports a
    # charge for an attempt we had already settled, and we cancelled that charge. It never
    # goes through SUCCEEDED, so the "one SUCCEEDED attempt" index is never in the way.
    A.SUCCEEDED: frozenset({A.REFUNDED_DUPLICATE}),
    A.DECLINED_SOFT: frozenset({A.REFUNDED_DUPLICATE}),
    A.DECLINED_HARD: frozenset({A.REFUNDED_DUPLICATE}),
    A.FAILED_NOT_CHARGED: frozenset({A.REFUNDED_DUPLICATE}),
    A.VOIDED: frozenset({A.REFUNDED_DUPLICATE}),
    A.REFUNDED_DUPLICATE: frozenset(),
}

PAYMENT_TERMINAL = frozenset(s for s, nxt in PAYMENT_TRANSITIONS.items() if not nxt)
# The outcome of the charge is not known yet: the engine must not move past such an attempt.
ATTEMPT_UNRESOLVED = frozenset({A.STARTED, A.UNKNOWN})
# Known not to hold money and cannot be charged later: safe to try the next provider.
ATTEMPT_MONEY_FREE = frozenset(
    {A.DECLINED_SOFT, A.DECLINED_HARD, A.FAILED_NOT_CHARGED, A.VOIDED, A.REFUNDED_DUPLICATE}
)


class TransitionKind(StrEnum):
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


class IllegalTransition(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Transition[S: StrEnum]:
    current: S
    target: S
    kind: TransitionKind

    @property
    def applied(self) -> bool:
        return self.kind is TransitionKind.APPLIED

    @property
    def new_status(self) -> S:
        """Status after the transition (unchanged unless APPLIED)."""
        return self.target if self.applied else self.current

    def require(self) -> "Transition[S]":
        """Raise unless the move was applied. For engine code paths."""
        if self.kind is not TransitionKind.APPLIED:
            raise IllegalTransition(f"{self.current} -> {self.target}: {self.kind}")
        return self


def _decide[S: StrEnum](graph: Mapping[S, frozenset[S]], current: S, target: S) -> Transition[S]:
    if current == target:
        return Transition(current, target, TransitionKind.DUPLICATE)
    if target in graph[current]:
        return Transition(current, target, TransitionKind.APPLIED)
    return Transition(current, target, TransitionKind.REJECTED)


def transition_payment(current: PaymentStatus, target: PaymentStatus) -> Transition[PaymentStatus]:
    return _decide(PAYMENT_TRANSITIONS, current, target)


def transition_attempt(current: AttemptStatus, target: AttemptStatus) -> Transition[AttemptStatus]:
    return _decide(ATTEMPT_TRANSITIONS, current, target)
