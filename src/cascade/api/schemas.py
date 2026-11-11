from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from cascade.domain.entities import Attempt, Payment

Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Country = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
Bin = Annotated[str, StringConstraints(pattern=r"^\d{6,8}$")]


class PaymentMethodIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=256)  # opaque token, never a PAN
    bin: Bin | None = None
    country: Country | None = None


class PaymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_minor: StrictInt = Field(gt=0)  # integer minor units; floats are rejected
    currency: Currency
    payment_method: PaymentMethodIn


class AttemptOut(BaseModel):
    id: UUID
    seq: int
    provider: str
    status: str
    reason: str | None
    raw_code: str | None
    provider_txn_id: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None

    @classmethod
    def of(cls, a: Attempt) -> "AttemptOut":
        dur = int((a.finished_at - a.started_at).total_seconds() * 1000) if a.finished_at else None
        return cls(
            id=a.id,
            seq=a.seq,
            provider=a.provider,
            status=a.status.value,
            reason=a.reason,
            raw_code=a.raw_code,
            provider_txn_id=a.provider_txn_id,
            started_at=a.started_at,
            finished_at=a.finished_at,
            duration_ms=dur,
        )


class PaymentOut(BaseModel):
    """Never contains the payment method token."""

    id: UUID
    status: str
    amount_minor: int
    currency: str
    route: list[str]
    failure_reason: str | None
    needs_review: bool
    created_at: datetime
    attempts: list[AttemptOut]

    @classmethod
    def of(cls, p: Payment, attempts: list[Attempt]) -> "PaymentOut":
        return cls(
            id=p.id,
            status=p.status.value,
            amount_minor=p.amount_minor,
            currency=p.currency,
            route=list(p.route),
            failure_reason=p.failure_reason,
            needs_review=p.needs_review,
            created_at=p.created_at,
            attempts=[AttemptOut.of(a) for a in attempts],
        )
