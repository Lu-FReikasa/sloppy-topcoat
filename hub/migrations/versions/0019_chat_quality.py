"""chat quality levels (E3-04): per conversation and per user, and the verification of an answer

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # fast | balanced | thorough; NULL = the user's default
    op.add_column("conversations", sa.Column("quality", sa.String(12), nullable=True))
    op.add_column(
        "user_settings",
        sa.Column("chat_quality", sa.String(12), nullable=False, server_default="balanced"),
    )
    # the second look at a checked answer by the big model: {ok, issues, model, at}
    op.add_column("messages", sa.Column("verification", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "verification")
    op.drop_column("user_settings", "chat_quality")
    op.drop_column("conversations", "quality")
