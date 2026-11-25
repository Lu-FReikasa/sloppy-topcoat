"""wiki pages: Topcoat's own wiki on top of the Ablage

Revision ID: wk02_wiki_pages
Revises: wk01_outline_out
Create Date: 2026-11-12

A wiki page is a report of the application ``wiki`` (versions, status, release) with a place in
the tree: a section, a title and the path ``section/title``, unique per person.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "wk02_wiki_pages"
down_revision: str | None = "wk01_outline_out"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wiki_pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("section", sa.String(80), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "source_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_nodes.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "path", name="uq_wiki_pages_user_path"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM reports WHERE application = 'wiki'")
    op.drop_table("wiki_pages")
