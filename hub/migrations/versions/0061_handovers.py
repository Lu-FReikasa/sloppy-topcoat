"""handovers: one application hands a request to another (PLATFORM.md 3.8)

Revision ID: 0061
Revises: 0060
Create Date: 2026-10-22

Additive: one table. The dossier is what the person saw before the click; its hash keeps it
unchanged once it was sent.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "handovers",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_application", sa.String(40), nullable=False),
        sa.Column("to_application", sa.String(40), nullable=False),
        sa.Column("conversation_id", UUID, sa.ForeignKey("conversations.id", ondelete="SET NULL")),
        sa.Column(
            "target_conversation_id", UUID, sa.ForeignKey("conversations.id", ondelete="SET NULL")
        ),
        sa.Column("task_id", UUID),
        sa.Column("dossier", JSONB, nullable=False),
        sa.Column("dossier_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="proposed"),
        sa.Column("result_summary", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_handovers_user_id", "handovers", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_handovers_user_id", table_name="handovers")
    op.drop_table("handovers")
