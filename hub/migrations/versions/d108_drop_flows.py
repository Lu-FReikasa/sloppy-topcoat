"""the flows are gone: their tables and columns are dropped

Revision ID: d108_drop_flows
Revises: d107_rating_variant
Create Date: 2026-11-06

The flow system (E7-05, D59) is removed: the chat policy is a setting (``chat_policy`` in
``hub_settings``), the background tasks read the hub's settings, and programs are block programs.
The twelve flows (all built in, none chosen as the chat policy or for a background task) were
exported to the private docs folder first. This drops ``flow_runs``, ``flow_versions``, ``flows``,
the conversations' ``flow_id`` and the records' ``flow_run_id``, and the settings that chose a flow.
The downgrade brings the empty structures back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d108_drop_flows"
down_revision: str | None = "d107_rating_variant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.drop_column("conversations", "flow_id")
    op.drop_column("job_records", "flow_run_id")
    op.drop_index("ix_flow_runs_user_started", table_name="flow_runs")
    op.drop_table("flow_runs")
    op.drop_table("flow_versions")
    op.drop_table("flows")
    op.execute(
        "DELETE FROM hub_settings WHERE key = 'chat_policy_flow' OR key LIKE 'background_flow:%'"
    )


def downgrade() -> None:
    op.create_table(
        "flows",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("graph", JSONB, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("updated_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "flow_versions",
        sa.Column("flow_id", UUID, sa.ForeignKey("flows.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("graph", JSONB, nullable=False),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("saved_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column(
            "saved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "flow_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("flow_id", UUID, sa.ForeignKey("flows.id", ondelete="SET NULL")),
        sa.Column("flow_name", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("question", sa.Text(), nullable=False, server_default=""),
        sa.Column("graph", JSONB, nullable=False),
        sa.Column("output", JSONB),
        sa.Column("error", sa.Text()),
        sa.Column("nodes", JSONB, nullable=False),
        sa.Column("activity", JSONB, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_flow_runs_user_started", "flow_runs", ["user_id", "started_at"])
    op.add_column("job_records", sa.Column("flow_run_id", sa.String(64)))
    op.add_column(
        "conversations",
        sa.Column("flow_id", UUID, sa.ForeignKey("flows.id", ondelete="SET NULL"), nullable=True),
    )
