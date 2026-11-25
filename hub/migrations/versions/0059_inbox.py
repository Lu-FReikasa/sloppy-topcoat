"""inbox_items: what happened and wants to be read (PLATFORM.md 3.7, P1)

Revision ID: 0059
Revises: 0058
Create Date: 2026-10-22

Additive: one table. An item belongs to a person and an application, may be urgent (stays until it
is resolved), is filed when it was read (unless pinned) and is tied to its source so that it is
made once.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "inbox_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("application", sa.String(40), nullable=False, server_default="chat"),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("source_type", sa.String(30)),
        sa.Column("source_ref", sa.String(80)),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("ref", JSONB, nullable=False, server_default="{}"),
        sa.Column("urgent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("filed_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("user_id", "kind", "source_type", "source_ref", name="uq_inbox_source"),
    )
    op.create_index("ix_inbox_items_user_id", "inbox_items", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_inbox_items_user_id", table_name="inbox_items")
    op.drop_table("inbox_items")
