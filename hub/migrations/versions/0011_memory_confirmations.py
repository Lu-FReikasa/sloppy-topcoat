"""memory (E2-02 fix): confirmations are a counter and a timestamp, not a new version

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_items",
        sa.Column("confirmed_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("memory_items", sa.Column("last_confirmed_at", sa.DateTime(timezone=True)))
    # Versions that only said "confirmed again" were noise: fold them into the counter.
    op.execute(
        "UPDATE memory_items m SET confirmed_count = c.n, last_confirmed_at = c.last "
        "FROM (SELECT item_id, count(*) AS n, max(created_at) AS last FROM memory_versions "
        "WHERE reason IN ('confirmed again in conversation') "
        "OR reason LIKE 'near-duplicate merged:%' GROUP BY item_id) c WHERE c.item_id = m.id"
    )


def downgrade() -> None:
    op.drop_column("memory_items", "last_confirmed_at")
    op.drop_column("memory_items", "confirmed_count")
