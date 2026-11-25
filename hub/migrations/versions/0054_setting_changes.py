"""setting_changes: the history of the parameters an admin changes while the hub runs (E7-03, D58)

Revision ID: 0054
Revises: 0053
Create Date: 2026-10-21

Additive: one new table. Rows are written by ``live_settings`` (who changed which parameter, from
what to what; a reset has a null new value) and never edited.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "setting_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_setting_changes_key", "setting_changes", ["key"])


def downgrade() -> None:
    op.drop_index("ix_setting_changes_key", table_name="setting_changes")
    op.drop_table("setting_changes")
