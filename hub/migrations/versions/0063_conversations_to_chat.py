"""conversations from before applications belong to Chat (PLATFORM.md 3.1)

Revision ID: 0063
Revises: 0062
Create Date: 2026-10-23

Data only: a conversation without an application was started before applications existed and is
shown in the Chat window; from now on it also gets Chat's tools as its ceiling (app_scope.py).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE conversations SET application = 'chat' WHERE application IS NULL")


def downgrade() -> None:
    # which conversations had no application is not kept: they stay in Chat
    pass
