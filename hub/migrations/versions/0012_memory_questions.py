"""memory (E2-02, D19 extended): clarification questions, source of a version

The memory keeps itself in order; where it cannot tell what is true it asks the user. A
question belongs to one user and names the notes in doubt; a version can name its source
(conversation, question, evidence) next to its reason.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_versions", sa.Column("source", JSONB(), nullable=False, server_default="{}")
    )
    op.create_table(
        "memory_questions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_ids", JSONB(), nullable=False),
        sa.Column("topic", sa.String(50), nullable=False, server_default=""),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="open"),
        sa.Column("context", JSONB(), nullable=False, server_default="{}"),
        sa.Column("answer", JSONB()),
        sa.Column("resolution", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('open', 'answered', 'dismissed')", name="memory_questions_status_check"
        ),
    )
    op.create_index("ix_memory_questions_user_id", "memory_questions", ["user_id"])


def downgrade() -> None:
    op.drop_table("memory_questions")
    op.drop_column("memory_versions", "source")
