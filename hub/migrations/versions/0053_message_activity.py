"""messages.activity: what the hub did to produce an answer, in order (E7-01, D56)

Revision ID: 0053
Revises: 0052
Create Date: 2026-10-21

Additive: one nullable JSONB column. A list of items (thought, plan line, tool call with result,
check, retry, note from the person) as ``activity.py`` writes them. Rows written before stay NULL
and show no activity.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages", sa.Column("activity", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("messages", "activity")
