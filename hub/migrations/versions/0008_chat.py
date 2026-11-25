"""chat (E2-01): conversations, messages, user settings, hub settings

Every personal table carries user_id (D8, D20).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("model", sa.String(255)),
        sa.Column("node", sa.String(63)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.String(255)),
        sa.Column("node", sa.String(63)),
        sa.Column("fallback_from", sa.String(63)),
        sa.Column("status", sa.String(16), nullable=False, server_default="complete"),
        sa.Column("error", sa.Text()),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column("first_token_ms", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("context_trimmed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="messages_role_check"),
        sa.CheckConstraint(
            "status IN ('complete', 'stopped', 'error')", name="messages_status_check"
        ),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id", "id"])
    op.create_index("ix_messages_user_id", "messages", ["user_id"])
    op.create_table(
        "user_settings",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("system_prompt", sa.Text()),
        sa.Column("temperature", sa.Float()),
        sa.Column("default_model", sa.String(255)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "hub_settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", JSONB(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("hub_settings")
    op.drop_table("user_settings")
    op.drop_index("ix_messages_user_id", table_name="messages")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_table("conversations")
