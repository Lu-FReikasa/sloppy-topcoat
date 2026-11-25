"""reset the measured generation speeds (SCH-02)

Revision ID: sch02_speeds
Revises: ns01_releases
Create Date: 2026-10-30

The generation speed of real answers was computed from the first visible token while the node
counts the thinking in its completion tokens, so long thoughts gave speeds ten to thirty times
too high (the baseline of 2026-10-30: 730 t/s stored against 72 t/s from the benchmark). The
values and their sample counts are emptied; they fill again from the next real answers, now
measured from the first token of any kind. Load speeds were never stored (the size of the model
was not passed on); they start to fill with the fix and are left alone.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "sch02_speeds"
down_revision: str | None = "ns01_releases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE node_speeds SET tg_tps = NULL, tg_samples = 0")


def downgrade() -> None:
    """The wrong values are not worth keeping: nothing to restore."""
