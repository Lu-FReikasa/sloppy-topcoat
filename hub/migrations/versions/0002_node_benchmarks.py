"""node_benchmarks: llama-bench results per node (E1-02)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nodes belong to the installation, not to a person: no user_id (like node_events).
    op.create_table(
        "node_benchmarks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "node_id",
            UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("benchmark_id", UUID(as_uuid=True), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("backend", sa.String(20), nullable=False),
        sa.Column("threads", sa.Integer(), nullable=False),
        sa.Column("n_gpu_layers", sa.Integer()),
        sa.Column("pp_tps", sa.Float(), nullable=False),
        sa.Column("tg_tps", sa.Float(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_node_benchmarks_node_id", "node_benchmarks", ["node_id"])


def downgrade() -> None:
    op.drop_table("node_benchmarks")
