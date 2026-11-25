"""agent_tasks: the chat a task was handed over from, and the time it may wait for a fast node

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-04

Additive: two nullable columns and one more allowed message kind.
Tasks without them behave as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_tasks",
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "agent_tasks", sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_agent_tasks_conversation_id", "agent_tasks", ["conversation_id"])
    # a chat message may say "handed over to the task queue"
    op.drop_constraint("messages_kind_check", "messages", type_="check")
    op.create_check_constraint(
        "messages_kind_check", "messages", "kind IN ('say', 'progress', 'report', 'handoff')"
    )


def downgrade() -> None:
    op.execute("UPDATE messages SET kind = 'say' WHERE kind = 'handoff'")
    op.drop_constraint("messages_kind_check", "messages", type_="check")
    op.create_check_constraint(
        "messages_kind_check", "messages", "kind IN ('say', 'progress', 'report')"
    )
    op.drop_index("ix_agent_tasks_conversation_id", table_name="agent_tasks")
    op.drop_column("agent_tasks", "deadline_at")
    op.drop_column("agent_tasks", "conversation_id")
