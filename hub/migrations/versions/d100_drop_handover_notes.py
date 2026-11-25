"""the hand-over notes (episodes) are gone (D100)

Revision ID: d100_drop_handover_notes
Revises: sch03_job_records
Create Date: 2026-11-04

A new chat reads the key terms of the running summaries of the latest chats now (chat_digests),
not a note of its own. The rows of kind ``episode`` in ``memory_items`` (and the runs that wrote
them) are deleted; their versions, tags and links go with them by the foreign keys. Nothing
reads them any more and the summaries they were made of are in ``chat_digests``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d100_drop_handover_notes"
down_revision: str | None = "sch03_job_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM memory_items WHERE kind = 'episode'")
    op.execute("DELETE FROM memory_runs WHERE kind = 'episode'")


def downgrade() -> None:
    # the notes are not restorable and nothing reads them: nothing to put back
    pass
