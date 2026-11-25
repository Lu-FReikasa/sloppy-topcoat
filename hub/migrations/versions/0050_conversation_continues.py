"""conversations.continues_id: a section of the continuous conversation is a chat of its own

Revision ID: 0050
Revises: 0049
Create Date: 2026-10-11

Additive: one nullable column pointing at the chat this one continues. The chain of such chats is
the user's continuous conversation; every link is an ordinary chat (archive, search, export,
delete). Deleting a chat leaves the next one standing (SET NULL). Rows written before stay NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "continues_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("conversations_continues_idx", "conversations", ["continues_id"])


def downgrade() -> None:
    op.drop_index("conversations_continues_idx", table_name="conversations")
    op.drop_column("conversations", "continues_id")
