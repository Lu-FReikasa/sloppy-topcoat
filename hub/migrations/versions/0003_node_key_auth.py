"""nodes: drop credential_hash, nodes authenticate with their ed25519 key (E1-03, D12)

Public keys stay, but nodes paired before this revision know the hub only by its old
plain-http URL and have no bearer token any more: pair them again against the https URL
(protocol v0 draft change, docs/PROTOCOL.md).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("nodes", "credential_hash")


def downgrade() -> None:
    # The old bearer credentials are gone; nodes have to pair again after a downgrade too.
    op.add_column("nodes", sa.Column("credential_hash", sa.String(64), nullable=True))
    op.create_unique_constraint("nodes_credential_hash_key", "nodes", ["credential_hash"])
