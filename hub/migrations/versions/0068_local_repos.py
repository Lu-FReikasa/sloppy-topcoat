"""local repositories of the dev workshop (ARCHITECTURE D76)

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-24

A repository is ``external`` (it lives elsewhere, GitHub; the assistant only reads it) or
``local`` (its origin lives in the workshop of a dev node, where the assistant edits and tests it);
a local repository may be a clone of an external one, its upstream. Every existing row is external.
Dev nodes get the action ``workshop.manage`` (the person's own steps on local repositories, from
the hub's page) and the capabilities it runs, plus ``repo.branches`` and ``repo.commit``; a node
that does not offer them (an older program) simply does not list them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from topcoat_hub.configmigrate import add_default_capabilities, add_role_actions

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)

CAPABILITIES = [
    "repo.branches",
    "repo.commit",
    "workshop.create",
    "workshop.clone",
    "workshop.branch",
    "workshop.merge",
    "workshop.publish",
    "workshop.pull",
    "workshop.remove",
]


def upgrade() -> None:
    op.add_column(
        "dev_repos",
        sa.Column("kind", sa.String(10), nullable=False, server_default="external"),
    )
    op.add_column(
        "dev_repos",
        sa.Column("node_id", UUID, sa.ForeignKey("nodes.id", ondelete="SET NULL")),
    )
    op.add_column(
        "dev_repos",
        sa.Column("upstream_id", UUID, sa.ForeignKey("dev_repos.id", ondelete="SET NULL")),
    )
    conn = op.get_bind()
    add_role_actions(conn, ["workshop.manage"], "0068_local_repos", role="dev")
    add_default_capabilities(conn, CAPABILITIES, "0068_local_repos", only_role="dev")


def downgrade() -> None:
    # the names stay in the node configs: an action or capability a node does not know is refused
    op.drop_column("dev_repos", "upstream_id")
    op.drop_column("dev_repos", "node_id")
    op.drop_column("dev_repos", "kind")
