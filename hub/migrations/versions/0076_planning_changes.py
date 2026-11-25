"""planning_changes: changes the Projekte chat prepared and the person confirms

Revision ID: 0076
Revises: 0075
Create Date: 2026-10-28

A chat never changes the planning data by itself (D15): the exact call is kept here until the person
applies or refuses it with one click.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "planning_changes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("capability", sa.String(60), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False),
        sa.Column("preview", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("result", postgresql.JSONB()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_planning_changes_user_id", "planning_changes", ["user_id"])
    op.create_index("ix_planning_changes_task_id", "planning_changes", ["task_id"])


def downgrade() -> None:
    op.drop_table("planning_changes")
