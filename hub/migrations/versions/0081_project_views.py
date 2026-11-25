"""saved views: a filter, a sort and a layout, per person (PPH-04, 04c)

Revision ID: 0081
Revises: 0080
Create Date: 2026-10-29

A view is a person's own: a title, a type (list, table, board, calendar, gantt), a filter in the
language of ``project_filter.py``, a sort rule, a layout (columns, board columns, ranges) and a
place in the list. A view with a ``project_id`` belongs to that project and goes with it; one
without works across all of the person's projects.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_views",
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
        ),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("type", sa.String(10), nullable=False),
        sa.Column("filter", sa.Text, nullable=False, server_default=""),
        sa.Column("sort", sa.String(200), nullable=False, server_default=""),
        sa.Column("config", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("favorite", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.Column("builtin", sa.String(20)),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.CheckConstraint(
            "type IN ('list', 'table', 'board', 'calendar', 'gantt')", name="ck_project_views_type"
        ),
    )
    op.create_index("ix_project_views_user_id", "project_views", ["user_id"])
    op.create_index("ix_project_views_project_id", "project_views", ["project_id"])
    # a seeded view exists once per person
    op.create_index(
        "uq_project_views_builtin",
        "project_views",
        ["user_id", "builtin"],
        unique=True,
        postgresql_where=sa.text("builtin IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("project_views")
