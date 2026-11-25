"""reports: a thorough research as a versioned document (D102)

Revision ID: d102_reports
Revises: d100_drop_handover_notes
Create Date: 2026-11-04

``reports`` holds the document (title, the question, status draft / reviewed / released, the
current version, the task and the chat it came from), ``report_versions`` its immutable versions
(Markdown, metadata, a hash, the parent version and the kind of change) and ``report_comments``
the comments on a version, anchored at a chapter. Every row carries ``user_id`` (D20).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d102_reports"
down_revision: str | None = "d100_drop_handover_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application", sa.String(40), nullable=False, server_default="research"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("question", sa.Text(), nullable=False, server_default=""),
        sa.Column("language", sa.String(2), nullable=False, server_default="en"),
        sa.Column("depth", sa.String(10), nullable=False, server_default="thorough"),
        sa.Column("status", sa.String(12), nullable=False, server_default="draft"),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("released_version", sa.Integer()),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_tasks.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('draft', 'reviewed', 'released')", name="ck_reports_status"),
    )
    op.create_index("ix_reports_user_id", "reports", ["user_id", "created_at"])
    op.create_index("ix_reports_task_id", "reports", ["task_id"])

    op.create_table(
        "report_versions",
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("parent_version", sa.Integer()),
        sa.Column("kind", sa.String(12), nullable=False, server_default="agent"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_report_versions_user_id", "report_versions", ["user_id"])

    op.create_table(
        "report_comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("anchor", sa.String(120), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author", sa.String(12), nullable=False, server_default="person"),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_report_comments_report_id", "report_comments", ["report_id", "created_at"])


def downgrade() -> None:
    op.drop_table("report_comments")
    op.drop_table("report_versions")
    op.drop_table("reports")
