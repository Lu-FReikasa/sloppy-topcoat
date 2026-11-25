"""ui_themes: custom themes of the desktop (docs/THEMES.md)

Revision ID: 0075
Revises: 0074
Create Date: 2026-10-28

A theme is data and a style sheet (never code); the three built-in ones live in the web build. A
custom theme is made and changed by an admin and chosen per person (``users.ui_prefs.look`` holds
its key; a key that no longer exists draws the terminal).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ui_themes",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("base", sa.String(20)),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("css", sa.Text(), nullable=False, server_default=""),
        sa.Column("rev", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("ui_themes")
