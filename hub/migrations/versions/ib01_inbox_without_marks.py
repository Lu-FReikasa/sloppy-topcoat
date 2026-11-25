"""the inbox without marks, filing, board and re-check (D111)

Revision ID: ib01_inbox_without_marks
Revises: sh01_sheets
Create Date: 2026-11-25

Destructive: the stored marks (``item_marks``), the pin and filing columns of ``inbox_items``, the
re-check link of a hand-over and the hand-overs that were re-checks are removed, and so are the
items a re-check left ("Changed: …"). Take a database dump first. The downgrade brings the empty
table and the columns back, not their content.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ib01_inbox_without_marks"
down_revision: str | None = "sh01_sheets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.execute("DELETE FROM inbox_items WHERE kind = 'recheck_changed'")
    op.execute("DELETE FROM handovers WHERE recheck_item_id IS NOT NULL")
    op.drop_column("handovers", "recheck_item_id")
    op.drop_index("ix_item_marks_item_id", table_name="item_marks")
    op.drop_index("ix_item_marks_user_id", table_name="item_marks")
    op.drop_table("item_marks")
    op.drop_column("inbox_items", "pinned")
    op.drop_column("inbox_items", "filed_at")


def downgrade() -> None:
    op.add_column("inbox_items", sa.Column("filed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "inbox_items",
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default="false"),
    )
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
