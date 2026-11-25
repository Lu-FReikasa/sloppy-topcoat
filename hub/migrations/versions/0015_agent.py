"""agent (E3-01a): tasks, transcript steps and evidence, all per user (D20)

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _user() -> sa.Column:
    return sa.Column(
        "user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


def _task() -> sa.Column:
    return sa.Column(
        "task_id",
        UUID(as_uuid=True),
        sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "agent_tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        _user(),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("model", sa.String(255)),
        sa.Column("via", sa.String(10), nullable=False, server_default="gui"),
        sa.Column("status", sa.String(12), nullable=False, server_default="queued"),
        sa.Column("stop_reason", sa.String(30)),
        sa.Column("error", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("diagnosis", JSONB()),
        sa.Column("step_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'canceled')",
            name="agent_tasks_status",
        ),
    )
    op.create_index("ix_agent_tasks_user_id", "agent_tasks", ["user_id"])
    op.create_table(
        "agent_steps",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        _task(),
        _user(),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("data", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("task_id", "seq", name="uq_agent_steps_task_seq"),
    )
    op.create_index("ix_agent_steps_task_id", "agent_steps", ["task_id"])
    op.create_index("ix_agent_steps_user_id", "agent_steps", ["user_id"])
    op.create_table(
        "agent_evidence",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        _task(),
        _user(),
        sa.Column("ref", sa.String(8), nullable=False),
        sa.Column("tag", sa.String(8), nullable=False),
        sa.Column("source", JSONB(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("task_id", "ref", name="uq_agent_evidence_task_ref"),
    )
    op.create_index("ix_agent_evidence_task_id", "agent_evidence", ["task_id"])
    op.create_index("ix_agent_evidence_user_id", "agent_evidence", ["user_id"])


def downgrade() -> None:
    op.drop_table("agent_evidence")
    op.drop_table("agent_steps")
    op.drop_table("agent_tasks")
