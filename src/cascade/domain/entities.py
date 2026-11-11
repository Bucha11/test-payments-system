"""Read models of persisted payments and attempts (plain data, no behaviour)."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from cascade.domain.enums import AttemptStatus, PaymentStatus


@dataclass(frozen=True, slots=True)
class Attempt:
    id: UUID
    payment_id: UUID
    seq: int
    provider: str
    status: AttemptStatus
    provider_txn_id: str | None
    raw_code: str | None
    raw_message: str | None
    reason: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class Payment:
    id: UUID
    idempotency_key: str
    request_hash: str
    amount_minor: int
    currency: str
    pm_token: str = field(repr=False)
    pm_bin: str | None
    pm_country: str | None
    status: PaymentStatus
    route: tuple[str, ...]
    deadline_at: datetime
    lease_until: datetime | None
    needs_review: bool
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NewPayment:
    id: UUID
    idempotency_key: str
    request_hash: str
    amount_minor: int
    currency: str
    pm_token: str = field(repr=False)
    pm_bin: str | None
    pm_country: str | None
    deadline_at: datetime  # created_at + cascade budget, from the engine's clock
