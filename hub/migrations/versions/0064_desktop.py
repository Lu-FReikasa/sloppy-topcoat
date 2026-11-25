"""the desktop replaces the shells and the looks (docs/DESKTOP.md, D70)

Revision ID: 0064
Revises: 0063
Create Date: 2026-10-23

The per-shell layout documents of a person and the presets of a workspace go, and so does the
interface look of a person. What a person has open lives in ``users.ui_prefs`` now (the hub clamps
it). The downgrade gives the tables and columns back empty: what was in them is not kept.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.drop_table("ui_layouts")
    op.drop_column("ui_templates", "layouts")
    op.drop_column("users", "look")


def downgrade() -> None:
    op.add_column("users", sa.Column("look", sa.String(16)))
    op.create_check_constraint(
        "users_look_check",
        "users",
        "look IN ('glas', 'kontrollraum', 'werkstatt', 'terminal', 'cyberpunk')",
    )
    op.add_column(
        "ui_templates",
        sa.Column("layouts", JSONB, nullable=False, server_default="{}"),
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
