"""agent proposals (E3-01b): the changes the agent proposes, run only after the owner's click

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-02

Additive: one new table, nothing existing changes. Every row carries ``user_id`` (D20).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("node_name", sa.String(63), nullable=False),
        sa.Column("capability", sa.String(100), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("effect", sa.Text(), nullable=False),
        sa.Column("who_notices", sa.Text(), nullable=False),
        sa.Column("way_back", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("tool_call_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'executed', 'failed', 'refused', 'expired')",
            name="agent_proposals_status_check",
        ),
    )
    op.create_index("ix_agent_proposals_user_id", "agent_proposals", ["user_id"])
    op.create_index("ix_agent_proposals_task", "agent_proposals", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_proposals_task", table_name="agent_proposals")
    op.drop_index("ix_agent_proposals_user_id", table_name="agent_proposals")
    op.drop_table("agent_proposals")
