"""the secretary's work cards (D112, issue #37)

Revision ID: so02_order_cards
Revises: so01_secretary_orders
Create Date: 2026-11-27

Adds two links to ``orders``: the chat message that said the task was handed on (the work card
stands under it) and the employee's own conversation that "talk to … directly" opened (a second
click opens the same one). Additive and nullable; the downgrade drops both columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "so02_order_cards"
down_revision: str | None = "so01_secretary_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "chat_message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "direct_conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("orders", "direct_conversation_id")
    op.drop_column("orders", "chat_message_id")
