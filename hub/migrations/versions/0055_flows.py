"""flows, flow_versions, flow_runs: the steps between a question and its answer as data (E7-05)

Revision ID: 0055
Revises: 0054
Create Date: 2026-10-21

Additive: three new tables. ``flows`` holds the current graph of a flow and its version number,
``flow_versions`` every saved state (never edited), ``flow_runs`` what a run did after it ended (the
graph it ran, the state of each node, the chain of activity, the result).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("ix_flow_runs_user_started", table_name="flow_runs")
    op.drop_table("flow_runs")
    op.drop_table("flow_versions")
    op.drop_table("flows")
