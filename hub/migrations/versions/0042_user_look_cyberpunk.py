"""users.look: allow the look "cyberpunk"

Revision ID: 0042
Revises: 0038
Create Date: 2026-10-07

Replaces the check constraint of 0030 by one that also lists "cyberpunk". No row is rewritten.

NOTE: 0039, 0040 and 0041 are used by other work; whoever merges later sets ``down_revision`` to
the then newest revision so there stays a single head.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0042"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("users_look_check", "users", type_="check")
    op.create_check_constraint(
        "users_look_check",
        "users",
        "look IN ('glas', 'kontrollraum', 'werkstatt', 'terminal', 'cyberpunk')",
    )


def downgrade() -> None:
    op.execute("UPDATE users SET look = NULL WHERE look = 'cyberpunk'")
    op.drop_constraint("users_look_check", "users", type_="check")
    op.create_check_constraint(
        "users_look_check",
        "users",
        "look IN ('glas', 'kontrollraum', 'werkstatt', 'terminal')",
    )
