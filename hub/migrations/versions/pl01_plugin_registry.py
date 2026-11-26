"""the registry of data plug-ins with versions and their audit (D105 step 2)

Revision ID: pl01_plugin_registry
Revises: ib01_inbox_without_marks
Create Date: 2026-11-26

Three new tables, nothing existing changes: ``plugins`` (one row per installed plug-in and the
version in force), ``plugin_versions`` (every version's manifest and hash) and ``plugin_changes``
(who installed, updated, rolled back, disabled or removed what). The built-in programs are
registered by the hub at its next start, not here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "pl01_plugin_registry"
down_revision: str | None = "ib01_inbox_without_marks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "plugins",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_table(
        "plugin_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "plugin_key",
            sa.String(40),
            sa.ForeignKey("plugins.key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("installed_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("plugin_key", "version", name="uq_plugin_versions"),
    )
    op.create_index("ix_plugin_versions_plugin_key", "plugin_versions", ["plugin_key"])
    op.create_table(
        "plugin_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("plugin_key", sa.String(40), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("from_version", sa.String(20)),
        sa.Column("to_version", sa.String(20)),
        sa.Column("sha256", sa.String(64)),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_index("ix_plugin_changes_plugin_key", "plugin_changes", ["plugin_key"])


def downgrade() -> None:
    op.drop_index("ix_plugin_changes_plugin_key", table_name="plugin_changes")
    op.drop_table("plugin_changes")
    op.drop_index("ix_plugin_versions_plugin_key", table_name="plugin_versions")
    op.drop_table("plugin_versions")
    op.drop_table("plugins")
