"""project templates: the structure of a project, saved to start new ones from (PPH-04, 04j)

Revision ID: 0084
Revises: 0083
Create Date: 2026-10-29

A template holds structure only (topics, tasks, labels, relative dates, relations), as one JSON
document in ``content``. It carries no event history and no personal facts: no reports, no answers,
no status. Personal (``user_id``); the name is unique per person.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0084"
down_revision: str | None = "0083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application", sa.String(40), nullable=False, server_default="project"),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.String(400), nullable=False, server_default=""),
        sa.Column("content", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "application", "name", name="uq_project_templates_name"),
    )
    op.create_index("ix_project_templates_user_id", "project_templates", ["user_id"])


def downgrade() -> None:
    op.drop_table("project_templates")
