"""which nodes and tool groups the assistant may use in a conversation (E3-04)

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # {"nodes": [name…] | null, "groups": [key…] | null}; NULL = everything the user may use
    op.add_column("conversations", sa.Column("tool_scope", JSONB(), nullable=True))
    # the last choice of the user: the scope new conversations start with
    op.add_column("user_settings", sa.Column("tool_scope", JSONB(), nullable=True))
    # the scope a task runs under (a chat check gets its conversation's)
    op.add_column("agent_tasks", sa.Column("scope", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_tasks", "scope")
    op.drop_column("user_settings", "tool_scope")
    op.drop_column("conversations", "tool_scope")
