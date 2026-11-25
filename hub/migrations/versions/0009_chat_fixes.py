"""chat fixes (E2-01 follow-up): address form setting, message status 'truncated'

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("address", sa.String(8)))
    op.create_check_constraint(
        "user_settings_address_check", "user_settings", "address IN ('du', 'sie')"
    )
    op.drop_constraint("messages_status_check", "messages", type_="check")
    op.create_check_constraint(
        "messages_status_check",
        "messages",
        "status IN ('complete', 'stopped', 'error', 'truncated')",
    )


def downgrade() -> None:
    op.execute("UPDATE messages SET status = 'stopped' WHERE status = 'truncated'")
    op.drop_constraint("messages_status_check", "messages", type_="check")
    op.create_check_constraint(
        "messages_status_check", "messages", "status IN ('complete', 'stopped', 'error')"
    )
    op.drop_constraint("user_settings_address_check", "user_settings", type_="check")
    op.drop_column("user_settings", "address")
