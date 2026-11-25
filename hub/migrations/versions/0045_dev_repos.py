"""dev_repos: repositories the assistant may look at (E6-01 S0, D52)

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-09

Additive: one new table. The token column holds a Fernet-sealed value, never a clear token.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dev_repos",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("full_name", sa.String(201), nullable=False),
        sa.Column("default_branch", sa.String(200), nullable=False, server_default="main"),
        sa.Column("mode", sa.String(16), nullable=False, server_default="read"),
        sa.Column("test_template", sa.String(16), nullable=False, server_default="none"),
        sa.Column("token_enc", sa.Text(), nullable=False),
        sa.Column("last_state", postgresql.JSONB(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "name", name="uq_dev_repos_user_name"),
    )
    op.create_index("ix_dev_repos_user_id", "dev_repos", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_dev_repos_user_id", table_name="dev_repos")
    op.drop_table("dev_repos")
