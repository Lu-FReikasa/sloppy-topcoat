"""sheets: chord sheets of the program Sheets

Revision ID: sh01_sheets
Revises: wk07_research_reads_wiki
Create Date: 2026-11-20

One row per sheet and person: the ChordPro text in sounding pitch (capo as ``{x_capo}``), the
instrument it is shown for, whether the guitar view shows capo shapes, and the sources a research
found (each as ChordPro) so they can be compared with the sheet later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "sh01_sheets"
down_revision: str | None = "wk07_research_reads_wiki"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sheets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("artist", sa.String(200), nullable=False, server_default=""),
        sa.Column("instrument", sa.String(20), nullable=False, server_default="guitar"),
        sa.Column("capo_view", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("key", sa.String(8)),
        sa.Column("capo", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("version_note", sa.String(300), nullable=False, server_default=""),
        sa.Column("chordpro", sa.Text(), nullable=False),
        sa.Column(
            "sources", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("instrument IN ('guitar', 'piano')", name="ck_sheets_instrument"),
        sa.CheckConstraint("capo >= 0 AND capo <= 12", name="ck_sheets_capo"),
    )
    op.create_index("ix_sheets_user_id", "sheets", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_sheets_user_id", table_name="sheets")
    op.drop_table("sheets")
