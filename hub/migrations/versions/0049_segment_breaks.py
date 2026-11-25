"""conversations.segment_breaks: the moments the user cut a conversation into a new segment

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-11

Additive: one nullable JSONB column (a list of ISO timestamps). A break works like a rest of
``segment_idle_seconds``: the messages written after it start a new segment. Rows written before
stay NULL (no breaks).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("segment_breaks", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "segment_breaks")
