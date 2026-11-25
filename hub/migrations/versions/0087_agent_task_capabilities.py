"""agent tasks can carry a list of the only capabilities they may use (PPH-05, 05c)

Revision ID: 0087
Revises: 0086
Create Date: 2026-10-29

A reviewer run reads a message a stranger may have written. It gets a short allow-list instead of
the tools of its application: the hub offers and runs nothing else for that task.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0087"
down_revision: str | None = "0086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_tasks", sa.Column("capabilities", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_tasks", "capabilities")
