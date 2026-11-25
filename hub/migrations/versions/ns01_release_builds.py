"""release builds: what the build service made, signed in the browser, then published (NS-01)

Revision ID: ns01_releases
Revises: 0088
Create Date: 2026-10-29

Numbered by name, not by number; it follows 0088 on the integrated tree. If another head appears,
``alembic merge heads`` or set ``down_revision`` to the newest one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ns01_releases"
down_revision: str | None = "0088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "release_builds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application", sa.String(10), nullable=False, server_default="admin"),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="release_repo"),
        sa.Column("ref", sa.String(200), nullable=False),
        sa.Column("commit", sa.String(40)),
        sa.Column("version", sa.String(40)),
        sa.Column("state", sa.String(12), nullable=False, server_default="requested"),
        sa.Column("node_id", postgresql.UUID(as_uuid=True)),
        sa.Column("node_name", sa.String(63)),
        sa.Column("changes", sa.Text(), nullable=False, server_default=""),
        sa.Column("artifacts", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("smoke", postgresql.JSONB()),
        sa.Column("rollout", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("log", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text()),
        sa.Column("channel", sa.String(32)),
        sa.Column("key_id", sa.String(16)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('requested', 'building', 'built', 'failed', 'published')",
            name="ck_release_builds_state",
        ),
        sa.CheckConstraint("kind IN ('node_release', 'hub_image')", name="ck_release_builds_kind"),
    )
    op.create_index("ix_release_builds_user_id", "release_builds", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_release_builds_user_id", table_name="release_builds")
    op.drop_table("release_builds")
