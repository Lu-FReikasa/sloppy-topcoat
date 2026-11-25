"""model ratings: the variant of the request a suite was run with

Revision ID: d107_rating_variant
Revises: d106_model_ratings
Create Date: 2026-11-05

``variant`` is the name of a preset of ``bench/variants.py`` ("" = the model as it is asked every
day). Only the ratings of the empty variant rank models; the others answer "does this setting help".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d107_rating_variant"
down_revision: str | None = "d106_model_ratings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_ratings", sa.Column("variant", sa.String(40), nullable=False, server_default="")
    )
    op.drop_index("ix_model_ratings_lookup", table_name="model_ratings")
    op.create_index(
        "ix_model_ratings_lookup", "model_ratings", ["model", "node", "suite", "variant"]
    )


def downgrade() -> None:
    op.drop_index("ix_model_ratings_lookup", table_name="model_ratings")
    op.create_index("ix_model_ratings_lookup", "model_ratings", ["model", "node", "suite"])
    op.drop_column("model_ratings", "variant")
