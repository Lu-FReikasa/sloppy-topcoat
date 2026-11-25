"""chat_digests: what the two looks at a finished chat produced

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-04

Additive: one new table, one row per conversation (overwritten when the chat goes on and rests
again). Every row belongs to a user and is only read through ``DigestStore``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_digests",
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
            unique=True,
        ),
        sa.Column("last_message_id", sa.BigInteger(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("passes", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="summarised"),
        sa.Column("learned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("author_model", sa.String(255)),
        sa.Column("author_node", sa.String(63)),
        sa.Column("checker_model", sa.String(255)),
        sa.Column("checker_node", sa.String(63)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('summarised', 'checked', 'no_checker')", name="chat_digests_status_check"
        ),
    )


def downgrade() -> None:
    op.drop_table("chat_digests")
