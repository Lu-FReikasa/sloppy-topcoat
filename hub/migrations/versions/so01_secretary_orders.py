"""the secretary's orders (D112, issue #36)

Revision ID: so01_secretary_orders
Revises: sh02_setlists
Create Date: 2026-11-26

Adds the table ``orders``: what the secretary handed on, to whom, its state and whether the
person asked to be told when it is done. Additive; the downgrade drops the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "so01_secretary_orders"
down_revision: str | None = "sh02_setlists"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("target", sa.String(20), nullable=False),
        sa.Column(
            "message_id",
            UUID,
            sa.ForeignKey("project_nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("topic", sa.String(300), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("report_when_done", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("orders_user_status_idx", "orders", ["user_id", "status"])
    op.create_index("ix_orders_conversation_id", "orders", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_conversation_id", table_name="orders")
    op.drop_index("orders_user_status_idx", table_name="orders")
    op.drop_table("orders")
