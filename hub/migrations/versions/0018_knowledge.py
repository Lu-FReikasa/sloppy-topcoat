"""knowledge base (E2-03 slim, D34): sources, files and searchable chunks

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "knowledge_sources",
        sa.Column("id", uuid, nullable=False),
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("visibility", sa.String(8), nullable=False),
        sa.Column("node_id", uuid),
        sa.Column("node_name", sa.String(63), nullable=False),
        sa.Column("root", sa.String(512), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("excluded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_sources_user_id", "knowledge_sources", ["user_id"])
    op.create_table(
        "knowledge_files",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_id", uuid, nullable=False),
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("mtime", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.ForeignKeyConstraint(["source_id"], ["knowledge_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "path", name="uq_knowledge_files_path"),
    )
    op.create_index("ix_knowledge_files_source_id", "knowledge_files", ["source_id"])
    op.create_index("ix_knowledge_files_user_id", "knowledge_files", ["user_id"])
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_id", uuid, nullable=False),
        sa.Column("file_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("heading", sa.Text(), nullable=False, server_default=""),
        sa.Column("ord", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("mtime", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("embedder", sa.String(100)),
        sa.ForeignKeyConstraint(["source_id"], ["knowledge_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["file_id"], ["knowledge_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # no fixed dimension (different embedders): an exact scan, like the memory's
    op.execute("ALTER TABLE knowledge_chunks ADD COLUMN embedding vector")
    op.create_index("ix_knowledge_chunks_source_id", "knowledge_chunks", ["source_id"])
    op.create_index("ix_knowledge_chunks_file_id", "knowledge_chunks", ["file_id"])
    op.create_index("ix_knowledge_chunks_user_id", "knowledge_chunks", ["user_id"])
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_fts ON knowledge_chunks "
        "USING gin (to_tsvector('simple', heading || ' ' || text))"
    )


def downgrade() -> None:
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_files")
    op.drop_table("knowledge_sources")
