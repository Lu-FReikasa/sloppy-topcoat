"""memory_items.application and tags: notes belong to an application or are shared (PLATFORM.md, P2)

Revision ID: 0060
Revises: 0059
Create Date: 2026-10-22

``application`` null means shared (about the person, in every application); a key means the note
belongs to that application only. Facts about the person (person, preference, style, rule) stay
shared; operational notes and episodes of a person move to ``chat``, where they were learned (every
conversation so far is a chat). The person sorts them further on the memory page; nothing is moved
silently into another application.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("memory_items", sa.Column("application", sa.String(40), nullable=True))
    op.add_column(
        "memory_items",
        sa.Column("tags", JSONB, nullable=False, server_default="[]"),
    )
    op.add_column("user_settings", sa.Column("memory_tag_rules", JSONB, nullable=True))
    op.create_index("ix_memory_items_user_application", "memory_items", ["user_id", "application"])
    op.get_bind().execute(
        sa.text(
            "UPDATE memory_items SET application = 'chat' "
            "WHERE scope = 'user' AND kind IN ('operational', 'episode')"
        )
    )


def downgrade() -> None:
    op.drop_column("user_settings", "memory_tag_rules")
    op.drop_index("ix_memory_items_user_application", table_name="memory_items")
    op.drop_column("memory_items", "tags")
    op.drop_column("memory_items", "application")
