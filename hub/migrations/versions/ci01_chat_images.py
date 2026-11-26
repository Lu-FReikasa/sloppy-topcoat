"""images a person gives the chat (D125)

Revision ID: ci01_chat_images
Revises: ib01_inbox_without_marks
Create Date: 2026-11-26

``chat_images``: an image sent with a user message of the chat, re-encoded by the hub (no
metadata) and kept for that person only (D20). Deleting the message or the conversation deletes it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "ci01_chat_images"
down_revision: str | None = "ib01_inbox_without_marks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_images",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content_type", sa.String(20), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_chat_images_user_id", "chat_images", ["user_id"])
    op.create_index("ix_chat_images_message_id", "chat_images", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_images_message_id", table_name="chat_images")
    op.drop_index("ix_chat_images_user_id", table_name="chat_images")
    op.drop_table("chat_images")
