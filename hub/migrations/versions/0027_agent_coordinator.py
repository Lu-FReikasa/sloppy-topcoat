"""coordinator tasks (E3-08): ``agent_tasks.kind`` and ``parent_id``

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-02

Additive: every existing task is ``single`` without a parent, so nothing changes for them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_tasks", sa.Column("kind", sa.String(12), server_default="single", nullable=False)
    )
    op.add_column(
        "agent_tasks",
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_agent_tasks_parent_id", "agent_tasks", ["parent_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_tasks_parent_id", table_name="agent_tasks")
    op.drop_column("agent_tasks", "parent_id")
    op.drop_column("agent_tasks", "kind")
