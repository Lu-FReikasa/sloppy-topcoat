"""Outline out: its knowledge sources and the notes mirrored from them are deleted

Revision ID: wk01_outline_out
Revises: na01_node_aliases
Create Date: 2026-11-12

Topcoat no longer reads the external Outline wiki (it has its own wiki now). Wrong data does more
harm than none, so the sources of kind ``outline`` go, with their files, passages and vectors (the
foreign keys cascade), and so do the memory notes the mirror made from their files.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "wk01_outline_out"
down_revision: str | None = "na01_node_aliases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM memory_items WHERE source_type = 'kb' AND source_ref->>'source' IN "
        "(SELECT id::text FROM knowledge_sources WHERE kind = 'outline')"
    )
    op.execute("DELETE FROM knowledge_sources WHERE kind = 'outline'")


def downgrade() -> None:
    # the deleted rows are gone; a source of Outline would have to be added again by hand
    pass
