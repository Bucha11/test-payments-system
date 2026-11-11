"""Interfaces the application layer needs from infrastructure."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from cascade.domain.entities import Attempt, NewPayment, Payment
from cascade.domain.enums import AttemptStatus, BreakerState, ChargeOutcome, PaymentStatus


class LostLease(Exception):
    """Our processing claim is gone (taken over or settled elsewhere). Stop, write nothing."""


@dataclass(frozen=True, slots=True)
class AttemptUpdate:
    reason: str | None = None
    raw_code: str | None = None
    raw_message: str | None = None
    provider_txn_id: str | None = None


class PaymentRepository(Protocol):
    """Every method is one short transaction. None of them is called across a network call.

    `token` = fencing token of the caller's claim. When given, the write happens only if
    the payment is still PROCESSING under that token (and the lease is renewed), otherwise
    LostLease is raised. Webhook paths pass token=None and rely on status conditions.
    """

    async def insert_payment(self, new: NewPayment) -> tuple[Payment, bool]: ...

    async def get(self, payment_id: UUID) -> Payment | None: ...

    async def attempts(self, payment_id: UUID) -> list[Attempt]: ...

    async def get_attempt(self, attempt_id: UUID) -> Attempt | None: ...

    async def set_route(self, payment_id: UUID, route: Sequence[str]) -> bool: ...

    async def claim(
        self, payment_id: UUID, token: UUID, lease_s: float
    ) -> PaymentStatus | None: ...

    async def start_attempt(
        self, payment_id: UUID, token: UUID, seq: int, provider: str
    ) -> Attempt: ...

    async def update_attempt(
        self,
        attempt_id: UUID,
        frm: AttemptStatus,
        to: AttemptStatus,
        data: AttemptUpdate,
        token: UUID | None,
    ) -> bool: ...

    async def succeed(
        self,
        payment_id: UUID,
        attempt_id: UUID,
        frm: AttemptStatus,
        data: AttemptUpdate,
        token: UUID | None,
    ) -> bool: ...

    async def finish_payment(
        self,
        payment_id: UUID,
        frm: PaymentStatus,
        to: PaymentStatus,
        token: UUID | None,
        *,
        reason: str | None = None,
        needs_review: bool = False,
        next_check_s: float = 0,
    ) -> bool: ...

    async def flag_needs_review(self, payment_id: UUID) -> None: ...

    async def due_for_recovery(self, created_grace_s: float, limit: int) -> list[UUID]: ...


class CircuitBreaker(Protocol):
    async def allow(self, provider: str) -> bool: ...

    async def record(self, provider: str, outcome: ChargeOutcome) -> None: ...


@dataclass(frozen=True, slots=True)
class BreakerSnapshot:
    state: BreakerState
    consecutive_failures: int
    seconds_until_half_open: float | None


class TaskQueue(Protocol):
    async def enqueue_reconcile(self, payment_id: UUID) -> None: ...


class Clock(Protocol):
    def __call__(self) -> datetime: ...
