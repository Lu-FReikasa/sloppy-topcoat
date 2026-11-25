"""batches of project tasks (PPH-04 plan 2026-11-08, S4)

Revision ID: pb01_project_batches
Revises: d108_drop_flows
Create Date: 2026-11-08

A batch is one approval of several task nodes of a project, run one after the other: the next task
starts when the last one's result fits its result form, and the batch stops at the first one that
does not. ``node_ids`` keeps the order; ``runs`` counts the runs started and ``max_runs`` is the
limit the approval named. Personal (``user_id``), like every project row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "pb01_project_batches"
down_revision: str | None = "d108_drop_flows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("node_ids", postgresql.JSONB(), nullable=False),
        sa.Column("max_runs", sa.Integer(), nullable=False),
        sa.Column("runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(10), nullable=False, server_default="active"),
        sa.Column("stop_reason", sa.String(60)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('active', 'done', 'stopped')", name="ck_project_batches_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("project_batches")
