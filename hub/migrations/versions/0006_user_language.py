"""users: GUI language preference (en | de, NULL = browser language)

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("language", sa.String(8), nullable=True))
    op.create_check_constraint("users_language_check", "users", "language IN ('en', 'de')")


def downgrade() -> None:
    op.drop_constraint("users_language_check", "users", type_="check")
    op.drop_column("users", "language")
