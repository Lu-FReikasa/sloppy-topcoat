"""planning application: projects, task tree, notes (brief PPH-01-planning-app, 01a)

Revision ID: 0069
Revises: 0068
Create Date: 2026-10-26

Three tables of the Planning application. Every row carries ``user_id`` and ``application``
(``planning``); queries filter by both. Nothing is hard-deleted by the application itself: a task
or project leaves the working set with status ``dropped``. Deleting a person cascades to their rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "planning_projects",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), index=True),
        sa.Column("application", sa.String(40), nullable=False, server_default="planning"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default="everyday"),
        sa.Column("goal", sa.Text, nullable=False, server_default=""),
        sa.Column("current_state", sa.Text, nullable=False, server_default=""),
        sa.Column("target_state", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "planning_tasks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "project_id",
            UUID,
            sa.ForeignKey("planning_projects.id", ondelete="CASCADE"),
            index=True,
        ),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), index=True),
        sa.Column("application", sa.String(40), nullable=False, server_default="planning"),
        sa.Column("parent_id", UUID, sa.ForeignKey("planning_tasks.id", ondelete="CASCADE")),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(12), nullable=False, server_default="todo"),
        sa.Column("depends_on", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "planning_notes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "project_id",
            UUID,
            sa.ForeignKey("planning_projects.id", ondelete="CASCADE"),
            index=True,
        ),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), index=True),
        sa.Column("application", sa.String(40), nullable=False, server_default="planning"),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("answer", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("planning_notes")
    op.drop_table("planning_tasks")
    op.drop_table("planning_projects")
