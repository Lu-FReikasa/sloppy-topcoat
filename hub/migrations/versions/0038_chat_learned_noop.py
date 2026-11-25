"""Placeholder for the production database, which stands at 0038

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-07

The production database was repaired by hand-built image ``local-f747d56-ehrlich5`` with a
revision numbered ``0038`` that created the table ``chat_learned`` (the learner ``chat_learn`` that
used it was replaced by the chat digest, ``chat_digest``). A database that stands at 0038 must find
this revision, otherwise the hub refuses to start. It changes nothing: a fresh database does not
need the table, and production keeps its (unused) copy until a cleanup migration drops it.
"""

from collections.abc import Sequence

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
