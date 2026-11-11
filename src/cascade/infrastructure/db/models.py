"""SQLAlchemy models. Schema of record is the Alembic migration; keep them in sync."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cascade.domain.enums import AttemptStatus, PaymentStatus


class Base(DeclarativeBase):
    pass


payment_status_enum = Enum(
    PaymentStatus, name="payment_status", values_callable=lambda e: [m.value for m in e]
)
attempt_status_enum = Enum(
    AttemptStatus, name="attempt_status", values_callable=lambda e: [m.value for m in e]
)


class PaymentRow(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_payments_amount_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_payments_currency_iso"),
        Index("ix_payments_status_lease", "status", "lease_until"),
        Index("ix_payments_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    request_hash: Mapped[str] = mapped_column(Text)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(CHAR(3))
    pm_token: Mapped[str] = mapped_column(Text)
    pm_bin: Mapped[str | None] = mapped_column(Text)
    pm_country: Mapped[str | None] = mapped_column(CHAR(2))
    status: Mapped[PaymentStatus] = mapped_column(payment_status_enum)
    route: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    needs_review: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AttemptRow(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("payment_id", "seq", name="uq_attempts_payment_seq"),
        # At most one successful charge per payment, enforced by the database.
        Index(
            "uq_attempts_one_success",
            "payment_id",
            unique=True,
            postgresql_where=text("status = 'SUCCEEDED'"),
        ),
        Index("ix_attempts_provider_started", "provider", "started_at"),
        CheckConstraint("seq >= 1", name="ck_attempts_seq_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(Text)
    status: Mapped[AttemptStatus] = mapped_column(attempt_status_enum)
    provider_txn_id: Mapped[str | None] = mapped_column(Text)
    raw_code: Mapped[str | None] = mapped_column(Text)
    raw_message: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookEventRow(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_webhook_events"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(Text)
    event_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
