"""chat_reviews.told_agent: the agent of the chat has been given this finding

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-14

Additive. The second look now runs right after each answer; a finding that is not "ok" goes into
the context of the chat's next answer once, so the agent can take it up. Reviews written before
count as told (they are not handed to a chat that moved on long ago).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_reviews",
        sa.Column("told_agent", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("chat_reviews", "told_agent", server_default=sa.false())


def downgrade() -> None:
    op.drop_column("chat_reviews", "told_agent")
