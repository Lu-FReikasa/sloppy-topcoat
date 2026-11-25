"""change sets: up to 100 operations the agent composed, applied by one click (PPH-04, 04g)

Revision ID: 0082
Revises: 0081
Create Date: 2026-10-29

A change set is immutable (``content_hash`` over the project and the operations); only its state,
the list of items that were applied, the result and the time of the decision change. The preview is
what a try-run in a rolled-back transaction would write.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_change_sets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application", sa.String(40), nullable=False, server_default="project"),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("operations", postgresql.JSONB, nullable=False),
        sa.Column("preview", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("applied", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("result", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_project_change_sets_user_id", "project_change_sets", ["user_id"])
    op.create_index("ix_project_change_sets_project_id", "project_change_sets", ["project_id"])
    op.create_index("ix_project_change_sets_task_id", "project_change_sets", ["task_id"])


def downgrade() -> None:
    op.drop_table("project_change_sets")
