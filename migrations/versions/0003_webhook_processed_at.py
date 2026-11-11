"""webhook_events.processed_at: dedupe on processed events only

Before, an event row was written first and applied second; a crash in between left a row
that made every redelivery a "duplicate", so the event was never applied. Now a row
without processed_at is applied again on redelivery (applying is idempotent).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("webhook_events", sa.Column("processed_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE webhook_events SET processed_at = received_at")


def downgrade() -> None:
    op.drop_column("webhook_events", "processed_at")
