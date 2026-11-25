"""model_transfers: direct model transfers between nodes (E2-07, D51)

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-07

Additive: one new table, and the two new default actions for existing nodes. The one-time token
of a transfer is never stored (it lives in the hub's memory only).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from topcoat_hub.configmigrate import add_default_actions

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIONS = ["models.send", "models.receive"]


def _node_fk(column: str) -> sa.Column:
    return sa.Column(
        column,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("nodes.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "model_transfers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _node_fk("source_node_id"),
        _node_fk("target_node_id"),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("target_name", sa.String(255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("move", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_model_transfers_user_id", "model_transfers", ["user_id", "created_at"])
    # Tool nodes (role tools) never move models: only nodes that run model actions get them.
    add_default_actions(op.get_bind(), ACTIONS, source="migration 0043", only_with="models.list")


def downgrade() -> None:
    op.drop_table("model_transfers")
