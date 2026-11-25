"""profiles and programs are not locked any more (docs/DESKTOP.md)

Revision ID: 0065
Revises: 0064
Create Date: 2026-10-23

``locked`` kept a person from rearranging the windows of a workspace. The desktop has no
arrangement to keep (the person opens and closes programs), so the flag had no effect left.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("ui_profiles", "locked")
    op.drop_column("ui_templates", "locked")


def downgrade() -> None:
    for table in ("ui_templates", "ui_profiles"):
        op.add_column(
            table, sa.Column("locked", sa.Boolean(), nullable=False, server_default="false")
        )
