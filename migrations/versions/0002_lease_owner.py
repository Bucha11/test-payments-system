"""payments.lease_owner: fencing token of the current processing claim

Every engine write checks `lease_owner = :token`, so a worker whose lease was taken over
(sweep after expiry, webhook settling the payment) cannot write anything afterwards.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("lease_owner", pg.UUID(as_uuid=True)))


def downgrade() -> None:
    op.drop_column("payments", "lease_owner")
