"""messages: one more kind, ``reaction`` (an emoji instead of an answer)

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-04

Additive: the check constraint on ``messages.kind`` allows ``reaction``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("messages_kind_check", "messages", type_="check")
    op.create_check_constraint(
        "messages_kind_check",
        "messages",
        "kind IN ('say', 'progress', 'report', 'handoff', 'reaction')",
    )


def downgrade() -> None:
    op.execute("UPDATE messages SET kind = 'say' WHERE kind = 'reaction'")
    op.drop_constraint("messages_kind_check", "messages", type_="check")
    op.create_check_constraint(
        "messages_kind_check", "messages", "kind IN ('say', 'progress', 'report', 'handoff')"
    )
