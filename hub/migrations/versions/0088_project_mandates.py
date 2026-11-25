"""mandates: the person's written release of one branch (PPH-05, 05d)

Revision ID: 0088
Revises: 0087
Create Date: 2026-10-29

A mandate is made by the person's click on a card that shows exactly its scope: one repository, one
branch ``topcoat/*``, the actions ``create_prototype`` and ``fix``, whether the hub may push that
branch, an expiry. It can be revoked. Orders that match it exactly are sent without a further click.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0088"
down_revision: str | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_mandates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application", sa.String(40), nullable=False, server_default="project"),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chain_id", postgresql.UUID(as_uuid=True)),
        sa.Column("target", sa.String(10), nullable=False, server_default="dev"),
        sa.Column("actions", postgresql.JSONB(), nullable=False),
        sa.Column("repo", sa.String(200), nullable=False),
        sa.Column("branch", sa.String(200), nullable=False),
        sa.Column("push", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("branch LIKE 'topcoat/%'", name="ck_project_mandates_branch"),
    )
    op.create_index("ix_project_mandates_user_id", "project_mandates", ["user_id"])
    op.create_index("ix_project_mandates_project_id", "project_mandates", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_project_mandates_project_id", table_name="project_mandates")
    op.drop_index("ix_project_mandates_user_id", table_name="project_mandates")
    op.drop_table("project_mandates")
