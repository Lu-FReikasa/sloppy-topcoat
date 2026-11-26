"""setlists: the order of songs for one evening (program Sheets)

Revision ID: sh02_setlists
Revises: ci01_chat_images
Create Date: 2026-11-26

``setlists`` (one per evening and person) and ``setlist_items`` (its songs in order: a sheet, as
that version or as the newest version of its song). Both carry ``user_id`` (D20); deleting a sheet
removes it from every setlist.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "sh02_setlists"
down_revision: str | None = "ci01_chat_images"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "setlists",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_setlists_user_id", "setlists", ["user_id"])
    op.create_table(
        "setlist_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "setlist_id",
            UUID(as_uuid=True),
            sa.ForeignKey("setlists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sheet_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sheets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("newest", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.UniqueConstraint("setlist_id", "position", name="uq_setlist_items_position"),
    )
    op.create_index("ix_setlist_items_setlist_id", "setlist_items", ["setlist_id"])
    op.create_index("ix_setlist_items_user_id", "setlist_items", ["user_id"])
    op.create_index("ix_setlist_items_sheet_id", "setlist_items", ["sheet_id"])


def downgrade() -> None:
    op.drop_index("ix_setlist_items_sheet_id", table_name="setlist_items")
    op.drop_index("ix_setlist_items_user_id", table_name="setlist_items")
    op.drop_index("ix_setlist_items_setlist_id", table_name="setlist_items")
    op.drop_table("setlist_items")
    op.drop_index("ix_setlists_user_id", table_name="setlists")
    op.drop_table("setlists")
