"""the internet switch of the chat: its default per user, and out of the tool groups (E3-04)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("internet", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # Web used to be one of the tool groups. A conversation that had picked groups WITHOUT web had
    # switched the internet off: that stays so (the new switch), and "web" leaves the group list.
    op.execute(
        """
        UPDATE conversations
        SET tool_scope = jsonb_set(tool_scope, '{internet}', 'false'::jsonb)
        WHERE jsonb_typeof(tool_scope -> 'groups') = 'array'
          AND NOT (tool_scope -> 'groups') ? 'web'
        """
    )
    op.execute(
        """
        UPDATE conversations
        SET tool_scope = jsonb_set(tool_scope, '{groups}', (tool_scope -> 'groups') - 'web')
        WHERE jsonb_typeof(tool_scope -> 'groups') = 'array'
        """
    )
    op.execute(
        """
        UPDATE user_settings
        SET tool_scope = jsonb_set(tool_scope, '{groups}', (tool_scope -> 'groups') - 'web')
        WHERE jsonb_typeof(tool_scope -> 'groups') = 'array'
        """
    )
    # what a user chose last is inherited by new conversations; the internet is not part of that
    op.execute(
        "UPDATE user_settings SET tool_scope = tool_scope - 'internet' "
        "WHERE tool_scope ? 'internet'"
    )


def downgrade() -> None:
    op.drop_column("user_settings", "internet")
