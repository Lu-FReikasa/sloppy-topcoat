"""chat_reviews: a second model's look at answers of recent chats

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-04

Additive: one new table. Every row belongs to a user and is only read through ``ReviewStore``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("verdict", sa.String(8), nullable=False),
        sa.Column("findings", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("reviewer_model", sa.String(255)),
        sa.Column("reviewer_node", sa.String(63)),
        sa.Column("author_model", sa.String(255)),
        sa.Column("author_node", sa.String(63)),
        sa.Column("seen", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("verdict IN ('ok', 'hint', 'error')", name="chat_reviews_verdict_check"),
    )


def downgrade() -> None:
    op.drop_table("chat_reviews")
