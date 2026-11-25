"""users: interface look preference (glas | kontrollraum | werkstatt | terminal, NULL = default)

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-03

Additive: one nullable column and a check constraint. No existing row is rewritten; NULL means
"the shipped default look", which the web interface resolves.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("look", sa.String(16), nullable=True))
    op.create_check_constraint(
        "users_look_check",
        "users",
        "look IN ('glas', 'kontrollraum', 'werkstatt', 'terminal')",
    )


def downgrade() -> None:
    op.drop_constraint("users_look_check", "users", type_="check")
    op.drop_column("users", "look")
