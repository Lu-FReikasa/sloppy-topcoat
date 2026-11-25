"""wiki pages in folders and categories

Revision ID: wk04_wiki_folders
Revises: wk03_wiki_tool_group
Create Date: 2026-11-12

A page's ``section`` becomes its folder: a path of names with ``/`` between them ("B Hub/Gateway"),
so it is wider. ``categories`` are free labels across the folders ("Entwurf", "Betrieb").
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "wk04_wiki_folders"
down_revision: str | None = "wk03_wiki_tool_group"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("wiki_pages", "section", type_=sa.String(240), existing_type=sa.String(80))
    op.alter_column("wiki_pages", "path", type_=sa.String(450), existing_type=sa.String(300))
    op.add_column(
        "wiki_pages",
        sa.Column("categories", JSONB(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("wiki_pages", "categories")
    op.alter_column("wiki_pages", "path", type_=sa.String(300), existing_type=sa.String(450))
    op.alter_column("wiki_pages", "section", type_=sa.String(80), existing_type=sa.String(240))
