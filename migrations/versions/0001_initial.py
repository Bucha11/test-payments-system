"""initial schema: payments, attempts, webhook_events

Revision ID: 0001
Revises:
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAYMENT_STATUSES = ("CREATED", "PROCESSING", "PENDING_RECONCILIATION", "SUCCEEDED", "FAILED")
ATTEMPT_STATUSES = (
    "STARTED",
    "SUCCEEDED",
    "DECLINED_SOFT",
    "DECLINED_HARD",
    "FAILED_NOT_CHARGED",
    "UNKNOWN",
    "VOIDED",
    "REFUNDED_DUPLICATE",
)

payment_status = pg.ENUM(*PAYMENT_STATUSES, name="payment_status", create_type=False)
attempt_status = pg.ENUM(*ATTEMPT_STATUSES, name="attempt_status", create_type=False)
tstz = sa.DateTime(timezone=True)


def upgrade() -> None:
    payment_status.create(op.get_bind())
    attempt_status.create(op.get_bind())

    op.create_table(
        "payments",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
        sa.Column("request_hash", sa.Text, nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("pm_token", sa.Text, nullable=False),
        sa.Column("pm_bin", sa.Text),
        sa.Column("pm_country", sa.CHAR(2)),
        sa.Column("status", payment_status, nullable=False),
        sa.Column("route", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("deadline_at", tstz, nullable=False),
        sa.Column("lease_until", tstz),
        sa.Column("needs_review", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("failure_reason", sa.Text),
        sa.Column("created_at", tstz, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", tstz, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("amount_minor > 0", name="ck_payments_amount_positive"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_payments_currency_iso"),
    )
    # Sweep/reconciliation scan: PROCESSING / PENDING_RECONCILIATION with expired lease.
    op.create_index("ix_payments_status_lease", "payments", ["status", "lease_until"])
    op.create_index("ix_payments_created_at", "payments", ["created_at"])

    op.create_table(
        "attempts",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "payment_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("payments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("status", attempt_status, nullable=False),
        sa.Column("provider_txn_id", sa.Text),
        sa.Column("raw_code", sa.Text),
        sa.Column("raw_message", sa.Text),
        sa.Column("reason", sa.Text),
        sa.Column("started_at", tstz, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", tstz),
        sa.UniqueConstraint("payment_id", "seq", name="uq_attempts_payment_seq"),
        sa.CheckConstraint("seq >= 1", name="ck_attempts_seq_positive"),
    )
    # The core invariant at the storage level: at most one SUCCEEDED attempt per payment.
    op.create_index(
        "uq_attempts_one_success",
        "attempts",
        ["payment_id"],
        unique=True,
        postgresql_where=sa.text("status = 'SUCCEEDED'"),
    )
    op.create_index("ix_attempts_provider_started", "attempts", ["provider", "started_at"])

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("payload", pg.JSONB, nullable=False),
        sa.Column("received_at", tstz, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "event_id", name="uq_webhook_events"),
    )


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.drop_table("attempts")
    op.drop_table("payments")
    attempt_status.drop(op.get_bind())
    payment_status.drop(op.get_bind())
