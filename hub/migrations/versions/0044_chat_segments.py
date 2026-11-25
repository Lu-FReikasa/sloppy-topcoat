"""chat_segments: summaries of the rests of one continuous conversation

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-07

Additive: one new table. A segment is the run of messages of a conversation between two rests of
at least ``segment_idle_seconds``; the boundaries are derived from the message times, the row only
stores the summary and hand-over written for a closed segment. Every row belongs to a user and
is only read through ``SegmentStore``.

NOTE: 0039 (schema cleanup) and 0040 (citation check) are reserved by other work; this revision
was 0041 on its own branch and now follows 0043 (model transfers). Whoever merges second sets
``down_revision`` to the then newest revision so there stays a single head.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_segments",
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
        ),
        sa.Column("first_message_id", sa.BigInteger(), nullable=False),
        sa.Column("last_message_id", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("author_model", sa.String(255)),
        sa.Column("author_node", sa.String(63)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("conversation_id", "first_message_id", name="chat_segments_start_uq"),
    )


def downgrade() -> None:
    op.drop_table("chat_segments")
