"""memory care (E3-05): kinds, verification, expiry, usefulness, archive, the run kind

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-05

Existing notes become ``person`` notes with no verification, never recalled or used, trust 1: their
dates stay, so nothing is struck or archived because of this migration (age alone never does).
Downgrade: an ``archived`` note becomes ``deleted`` (soft, restorable by the older code).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUS = "status IN ('active', 'superseded', 'disputed', 'deleted')"
NEW_STATUS = "status IN ('active', 'superseded', 'disputed', 'deleted', 'archived')"


def upgrade() -> None:
    op.add_column(
        "memory_items",
        sa.Column("kind", sa.String(12), nullable=False, server_default="person"),
    )
    op.add_column("memory_items", sa.Column("last_verified_at", sa.DateTime(timezone=True)))
    op.add_column(
        "memory_items",
        sa.Column("verified_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("memory_items", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column("memory_items", sa.Column("verify_with", JSONB()))
    op.add_column(
        "memory_items", sa.Column("trust", sa.Float(), nullable=False, server_default="1.0")
    )
    op.add_column(
        "memory_items",
        sa.Column("recalled_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "memory_items", sa.Column("used_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("memory_items", sa.Column("last_used_at", sa.DateTime(timezone=True)))
    op.add_column(
        "memory_items",
        sa.Column("feedback_positive", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "memory_items",
        sa.Column("feedback_negative", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("memory_items", sa.Column("care_checked_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "memory_items_kind_check", "memory_items", "kind IN ('person', 'preference', 'operational')"
    )
    op.drop_constraint("memory_items_status_check", "memory_items", type_="check")
    op.create_check_constraint("memory_items_status_check", "memory_items", NEW_STATUS)
    # a care run and a consolidation run share the table: the report says which one it is
    op.add_column(
        "memory_runs",
        sa.Column("kind", sa.String(16), nullable=False, server_default="consolidation"),
    )


def downgrade() -> None:
    op.drop_column("memory_runs", "kind")
    op.execute("UPDATE memory_items SET status = 'deleted' WHERE status = 'archived'")
    op.execute("UPDATE memory_versions SET status = 'deleted' WHERE status = 'archived'")
    op.drop_constraint("memory_items_status_check", "memory_items", type_="check")
    op.create_check_constraint("memory_items_status_check", "memory_items", OLD_STATUS)
    op.drop_constraint("memory_items_kind_check", "memory_items", type_="check")
    for column in (
        "care_checked_at",
        "feedback_negative",
        "feedback_positive",
        "last_used_at",
        "used_count",
        "recalled_count",
        "trust",
        "verify_with",
        "expires_at",
        "verified_count",
        "last_verified_at",
        "kind",
    ):
        op.drop_column("memory_items", column)
