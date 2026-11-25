"""memory_reports: what the cross-check of the program memories found (ARCHITECTURE D74)

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-24

Two programs may remember the same topic differently. The cross-check compares the memories and
reports a discrepancy to the program responsible for the topic (by the tag tree), which decides;
it may send a short notice to the program whose note it changed. The table holds both, and the
pairs that agreed (so they are not compared again until one of them changes).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "memory_reports",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("recipient", sa.String(40), nullable=False),
        sa.Column("sender", sa.String(40)),
        sa.Column("item_a", UUID, sa.ForeignKey("memory_items.id", ondelete="CASCADE")),
        sa.Column("item_b", UUID, sa.ForeignKey("memory_items.id", ondelete="CASCADE")),
        sa.Column("topic", sa.String(30), nullable=False, server_default=""),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("signature", sa.String(64), nullable=False, server_default=""),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("decision", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_memory_reports_user_recipient", "memory_reports", ["user_id", "recipient", "status"]
    )
    op.create_index("ix_memory_reports_signature", "memory_reports", ["user_id", "signature"])


def downgrade() -> None:
    op.drop_table("memory_reports")
