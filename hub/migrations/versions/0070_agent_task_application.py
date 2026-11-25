"""agent tasks know their application (brief PPH-01-planning-app, 01b)

Revision ID: 0070
Revises: 0069
Create Date: 2026-10-26

A run of the planning agent is a task with ``application = planning``. The column is nullable:
every existing task belongs to no application (its tools and memory are the ones it had).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_tasks", sa.Column("application", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_tasks", "application")
