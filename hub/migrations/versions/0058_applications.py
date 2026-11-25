"""applications: workspaces become Chat, Research, Development and Administration (PLATFORM.md, P1)

Revision ID: 0058
Revises: 0057
Create Date: 2026-10-22

``ui_templates.open`` names the modules a fresh arrangement opens (null = all of them, as before);
``conversations.application`` says in which application a conversation was started. The workspaces
``talk``, ``knowledge`` and ``homelab`` of the first layout release are replaced by ``chat``,
``research`` and ``admin``: profile grants move to the new keys, and the layouts people stored for
the removed workspaces are dropped (the new ones open as an empty chat window and the inbox).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
RENAMED = (("talk", "chat"), ("knowledge", "research"), ("homelab", "admin"))


def upgrade() -> None:
    op.add_column("ui_templates", sa.Column("open", JSONB, nullable=True))
    op.add_column("conversations", sa.Column("application", sa.String(40), nullable=True))
    op.create_index(
        "ix_conversations_user_application", "conversations", ["user_id", "application"]
    )
    conn = op.get_bind()
    for old, new in RENAMED:
        conn.execute(
            sa.text(
                "UPDATE ui_grants g SET key = :new WHERE g.kind = 'workspace' AND g.key = :old "
                "AND NOT EXISTS (SELECT 1 FROM ui_grants h WHERE h.profile_id = g.profile_id "
                "AND h.kind = 'workspace' AND h.key = :new)"
            ),
            {"old": old, "new": new},
        )
        conn.execute(
            sa.text("DELETE FROM ui_grants WHERE kind = 'workspace' AND key = :old"),
            {"old": old},
        )
        conn.execute(sa.text("DELETE FROM ui_layouts WHERE workspace = :old"), {"old": old})
        conn.execute(sa.text("DELETE FROM ui_templates WHERE builtin AND key = :old"), {"old": old})


def downgrade() -> None:
    op.drop_index("ix_conversations_user_application", table_name="conversations")
    op.drop_column("conversations", "application")
    op.drop_column("ui_templates", "open")
