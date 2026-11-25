"""the place a conversation was started from (Clipper at a window)

Revision ID: cv01_conversation_context
Revises: wk04_wiki_folders
Create Date: 2026-11-12

``conversations.context``: the window's view, its ids and its title, as the page sent them when
Clipper was attached (conversation_context.py). Null for every conversation before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "cv01_conversation_context"
down_revision: str | None = "wk04_wiki_folders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("context", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "context")
