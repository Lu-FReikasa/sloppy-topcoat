"""ui_templates, ui_layouts, ui_profiles, ui_grants; users.profile_id, users.ui_prefs (L1, D60)

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-22

Additive: four new tables and two new columns on ``users``. A person's layout documents live in
``ui_layouts`` (one row per person, workspace and shell, reached only with the ``user_id``); the
workspaces the hub or an admin publishes live in ``ui_templates`` (owned by the installation); a
profile is a named set of grants (``ui_profiles`` and ``ui_grants``). Nothing is seeded here: the
hub writes its built-in templates and profiles at every start (``topcoat_hub.ui.seeds``).

NOTE: renumber ``down_revision`` when this is merged behind other migrations, so that there stays
a single head.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
UUID = postgresql.UUID(as_uuid=True)
SYSTEM_ID = "00000000-0000-4000-8000-000000000003"


def upgrade() -> None:
    op.create_table(
        "ui_profiles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.String(60), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # at most one default profile
    op.create_index(
        "uq_ui_profiles_one_default",
        "ui_profiles",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_table(
        "ui_grants",
        sa.Column(
            "profile_id",
            UUID,
            sa.ForeignKey("ui_profiles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("kind", sa.String(10), primary_key=True),
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("effect", sa.String(5), nullable=False),
        sa.CheckConstraint("kind IN ('workspace', 'tool')", name="ui_grants_kind_check"),
        sa.CheckConstraint("effect IN ('allow', 'deny')", name="ui_grants_effect_check"),
    )
    op.create_table(
        "ui_templates",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column(
            "owner_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            server_default=SYSTEM_ID,
        ),
        sa.Column("rev", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("title_key", sa.String(80)),
        sa.Column("icon", sa.String(8), nullable=False, server_default=""),
        sa.Column("position", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("modules", JSONB, nullable=False),
        sa.Column("agent", JSONB, nullable=False, server_default="{}"),
        sa.Column("layouts", JSONB, nullable=False, server_default="{}"),
        sa.Column("requires_role", sa.String(20)),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("updated_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("requires_role IS NULL OR requires_role = 'admin'", name="ui_tpl_role"),
    )
    op.create_table(
        "ui_layouts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace", sa.String(40), nullable=False),
        sa.Column("shell", sa.String(12), nullable=False),
        sa.Column("doc", JSONB, nullable=False),
        sa.Column("rev", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("template_rev", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "workspace", "shell", name="uq_ui_layouts_user_ws_shell"),
    )
    op.add_column(
        "users",
        sa.Column("profile_id", UUID, sa.ForeignKey("ui_profiles.id", ondelete="SET NULL")),
    )
    op.add_column("users", sa.Column("ui_prefs", JSONB, nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("users", "ui_prefs")
    op.drop_column("users", "profile_id")
    op.drop_table("ui_layouts")
    op.drop_table("ui_templates")
    op.drop_table("ui_grants")
    op.drop_index("uq_ui_profiles_one_default", table_name="ui_profiles")
    op.drop_table("ui_profiles")
