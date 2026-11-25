"""node_speeds: speeds the hub measured in real answers and loads (per node and model)

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-04

Additive: one new table. The node choice uses these numbers before the benchmark ones.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "node_speeds",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("tg_tps", sa.Float(), nullable=True),
        sa.Column("tg_samples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("load_mb_s", sa.Float(), nullable=True),
        sa.Column("load_samples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("node_id", "model", name="uq_node_speeds_node_model"),
    )


def downgrade() -> None:
    op.drop_table("node_speeds")
