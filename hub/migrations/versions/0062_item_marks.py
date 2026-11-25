"""item_marks and the re-check link of a hand-over (PLATFORM.md 3.7)

Revision ID: 0062
Revises: 0061
Create Date: 2026-10-22

Additive: one table, one nullable column. A mark is data on an inbox item (a comment, a Post-It, a
sticker, a highlight); it never changes the item's text.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "item_marks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "item_id", UUID, sa.ForeignKey("inbox_items.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("author", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("text", sa.String(500), nullable=False, server_default=""),
        sa.Column("line", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_item_marks_user_id", "item_marks", ["user_id"])
    op.create_index("ix_item_marks_item_id", "item_marks", ["item_id"])
    op.add_column(
        "handovers",
        sa.Column("recheck_item_id", UUID, sa.ForeignKey("inbox_items.id", ondelete="SET NULL")),
    )


def downgrade() -> None:
    op.drop_column("handovers", "recheck_item_id")
    op.drop_index("ix_item_marks_item_id", table_name="item_marks")
    op.drop_index("ix_item_marks_user_id", table_name="item_marks")
    op.drop_table("item_marks")
