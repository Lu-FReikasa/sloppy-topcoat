"""message kinds (E3-07 D3): ``messages.kind`` (say | progress | report) and ``messages.report``

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-07

A message is a chat bubble (``say``), the line that shows which tool runs (``progress``, deleted
when the check ends) or the report card the hub builds from a check (``report``; the structure
lives in ``report``, the Markdown text in ``content``). Every message that exists is a ``say``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages", sa.Column("kind", sa.String(16), server_default="say", nullable=False)
    )
    op.add_column("messages", sa.Column("report", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(
        "messages_kind_check", "messages", "kind IN ('say', 'progress', 'report')"
    )


def downgrade() -> None:
    # a card has no place in the old model: its text stays as an ordinary message; a progress
    # line goes
    op.execute("DELETE FROM messages WHERE kind = 'progress'")
    op.drop_constraint("messages_kind_check", "messages", type_="check")
    op.drop_column("messages", "report")
    op.drop_column("messages", "kind")
