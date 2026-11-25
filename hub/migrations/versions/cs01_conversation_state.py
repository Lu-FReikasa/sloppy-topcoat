"""open, pinned and done conversations (Start gathers what is open)

Revision ID: cs01_conversation_state
Revises: wk05_chat_reads_wiki
Create Date: 2026-11-13

``conversations.pinned_at``: the person keeps the conversation open in Start, however old it is.
``conversations.done_at``: the person marked it done; new activity after it opens it again. A
conversation without activity for a day counts as done by itself (computed, not stored).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cs01_conversation_state"
down_revision: str | None = "wk05_chat_reads_wiki"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations", sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("conversations", sa.Column("done_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "done_at")
    op.drop_column("conversations", "pinned_at")
