"""planning: a request is queued as a task of the agent queue

Revision ID: 0073
Revises: 0072
Create Date: 2026-10-27

A request that is sent (under a free grant, or after the person confirmed it) becomes a task of
the agent queue for its target application. ``request_task_id`` links the report to that task.
Any node can take the task: the queue is the hub's table, not a machine.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        "planning_nodes",
        sa.Column(
            "request_task_id",
            UUID,
            sa.ForeignKey("agent_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_planning_nodes_request_task_id", "planning_nodes", ["request_task_id"])


def downgrade() -> None:
    op.drop_index("ix_planning_nodes_request_task_id", table_name="planning_nodes")
    op.drop_column("planning_nodes", "request_task_id")
