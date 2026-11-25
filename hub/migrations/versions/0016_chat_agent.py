"""chat uses the agent (E3-01d): where a task came from, and the task behind a chat answer

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # tasks | chat: a check a chat answer started is kept out of the list on the Tasks page
    op.add_column(
        "agent_tasks",
        sa.Column("origin", sa.String(10), nullable=False, server_default="tasks"),
    )
    op.add_column(
        "messages",
        sa.Column(
            "agent_task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("messages", "agent_task_id")
    op.drop_column("agent_tasks", "origin")
