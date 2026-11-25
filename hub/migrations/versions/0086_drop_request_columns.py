"""the requests live in the message columns; the four ``request_*`` columns go (PPH-05, 05b)

Revision ID: 0086
Revises: 0085
Create Date: 2026-10-29

Migration 0085 turned the requests of the old flow into ``project.request`` messages. From here on
the flow runs on the message columns (type, target, state, task, payload), so the old columns are
dropped. The downgrade brings them back and fills them from the messages, so a rollback keeps the
requests.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0086"
down_revision: str | None = "0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_project_nodes_request_task_id", table_name="project_nodes")
    for column in ("request_task_id", "request_state", "request_action", "request_target"):
        op.drop_column("project_nodes", column)


def downgrade() -> None:
    op.add_column("project_nodes", sa.Column("request_target", sa.String(20)))
    op.add_column("project_nodes", sa.Column("request_action", sa.String(30)))
    op.add_column("project_nodes", sa.Column("request_state", sa.String(12)))
    op.add_column(
        "project_nodes",
        sa.Column(
            "request_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_tasks.id", ondelete="SET NULL"),
        ),
    )
    op.create_index("ix_project_nodes_request_task_id", "project_nodes", ["request_task_id"])
    op.execute(
        """
        UPDATE project_nodes
           SET request_target = target_app,
               request_action = COALESCE(
                   payload ->> 'action',
                   CASE WHEN target_app = 'admin' THEN 'read_released' ELSE 'request_report' END),
               request_state = CASE
                   WHEN msg_state = 'awaiting_click' THEN 'awaiting'
                   WHEN msg_state IN ('rejected', 'stopped') THEN 'declined'
                   ELSE 'sent' END,
               request_task_id = task_id
         WHERE message_type = 'project.request'
        """
    )
