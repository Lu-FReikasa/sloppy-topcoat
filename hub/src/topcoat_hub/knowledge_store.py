"""Data access for the knowledge base (E2-03, D20).

A :class:`KnowledgeStore` belongs to one user and **every statement filters by what that user may
see**: a source is visible to its owner, and to everybody when it is ``shared``; a ``private``
source of somebody else is "not found", and so are its files and chunks. Writes need the
owner's id. Indexing runs with :class:`KnowledgeIndexer` (``knowledge.py``), which writes through
the same store.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.knowledge_rules import keywords
from topcoat_hub.memory_store import vector_literal
from topcoat_hub.models import KnowledgeChunk, KnowledgeFile, KnowledgeSource


def utcnow() -> datetime:
    return datetime.now(UTC)


class KnowledgeStore:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db = db
        self.user_id = user_id

    def _visible(self) -> Any:
        return or_(KnowledgeSource.user_id == self.user_id, KnowledgeSource.visibility == "shared")

    # --- sources -------------------------------------------------------------------------

    async def create_source(self, **values: Any) -> KnowledgeSource:
        source = KnowledgeSource(user_id=self.user_id, **values)
        self.db.add(source)
        await self.db.commit()
        return source

    async def sources(self) -> Sequence[KnowledgeSource]:
        rows = await self.db.scalars(
            select(KnowledgeSource).where(self._visible()).order_by(KnowledgeSource.name)
        )
        return rows.all()

    async def get_source(self, source_id: uuid.UUID) -> KnowledgeSource | None:
        """A source this user may see."""
        return await self.db.scalar(
            select(KnowledgeSource).where(KnowledgeSource.id == source_id, self._visible())
        )

    async def own_source(self, source_id: uuid.UUID) -> KnowledgeSource | None:
        """A source this user owns (only the owner changes or deletes it)."""
        return await self.db.scalar(
            select(KnowledgeSource).where(
                KnowledgeSource.id == source_id, KnowledgeSource.user_id == self.user_id
            )
        )

    async def update_source(self, source_id: uuid.UUID, **values: Any) -> None:
        await self.db.execute(
            update(KnowledgeSource)
            .where(KnowledgeSource.id == source_id, KnowledgeSource.user_id == self.user_id)
            .values(**values)
        )
        await self.db.commit()

    async def delete_source(self, source_id: uuid.UUID) -> bool:
        result = await self.db.execute(
            delete(KnowledgeSource).where(
                KnowledgeSource.id == source_id, KnowledgeSource.user_id == self.user_id
            )
        )
        await self.db.commit()
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def any_visible_chunk(self) -> bool:
        """Is there anything to search for this user (the agent is offered the tool only then)."""
        row = await self.db.scalar(
            select(KnowledgeChunk.id)
            .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
            .where(self._visible())
            .limit(1)
        )
        return row is not None

    # --- files and chunks (written by the owner's indexer) ---------------------------------

    async def files(self, source_id: uuid.UUID) -> dict[str, KnowledgeFile]:
        rows = await self.db.scalars(
            select(KnowledgeFile)
            .join(KnowledgeSource, KnowledgeSource.id == KnowledgeFile.source_id)
            .where(KnowledgeFile.source_id == source_id, self._visible())
        )
        return {f.path: f for f in rows}

    async def drop_files(self, source_id: uuid.UUID, paths: Sequence[str]) -> None:
        if paths:
            await self.db.execute(
                delete(KnowledgeFile).where(
                    KnowledgeFile.source_id == source_id,
                    KnowledgeFile.user_id == self.user_id,
                    KnowledgeFile.path.in_(list(paths)),
                )
            )
            await self.db.commit()

    async def replace_file(
        self,
        source_id: uuid.UUID,
        path: str,
        size: int,
        mtime: int,
        content_hash: str,
        blocked: list[str],
        chunks: list[tuple[int, str, str, list[float] | None, str | None]],
    ) -> KnowledgeFile:
        """Write a file's chunks anew: ``(ord, heading, text, embedding, embedder)`` each."""
        file = await self.db.scalar(
            select(KnowledgeFile).where(
                KnowledgeFile.source_id == source_id,
                KnowledgeFile.path == path,
                KnowledgeFile.user_id == self.user_id,
            )
        )
        if file is None:
            file = KnowledgeFile(source_id=source_id, user_id=self.user_id, path=path)
            self.db.add(file)
            await self.db.flush()
        else:
            await self.db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.file_id == file.id))
        file.size, file.mtime, file.content_hash = size, mtime, content_hash
        file.chunk_count, file.blocked = len(chunks), blocked
        for ord_, heading, body, vec, emb in chunks:
            self.db.add(
                KnowledgeChunk(
                    source_id=source_id,
                    file_id=file.id,
                    user_id=self.user_id,
                    path=path,
                    heading=heading,
                    ord=ord_,
                    text=body,
                    mtime=mtime,
                    embedding=vec,
                    embedder=emb,
                )
            )
        await self.db.commit()
        return file

    async def touch_file(self, file: KnowledgeFile, size: int, mtime: int) -> None:
        """Content unchanged although size or time moved: remember the new values."""
        await self.db.execute(
            update(KnowledgeFile)
            .where(KnowledgeFile.id == file.id, KnowledgeFile.user_id == self.user_id)
            .values(size=size, mtime=mtime)
        )
        await self.db.commit()

    async def unembedded(self, source_id: uuid.UUID, embedder: str) -> list[tuple[int, str, str]]:
        """Chunks of the owner's source that have no vector of ``embedder`` yet."""
        rows = await self.db.execute(
            select(KnowledgeChunk.id, KnowledgeChunk.heading, KnowledgeChunk.text).where(
                KnowledgeChunk.source_id == source_id,
                KnowledgeChunk.user_id == self.user_id,
                or_(KnowledgeChunk.embedder.is_(None), KnowledgeChunk.embedder != embedder),
            )
        )
        return [(r.id, r.heading, r.text) for r in rows]

    async def embedded_counts(self, embedder: str) -> dict[uuid.UUID, int]:
        """Per source of this owner: the chunks that carry a vector of ``embedder``."""
        rows = await self.db.execute(
            select(KnowledgeChunk.source_id, func.count())
            .where(KnowledgeChunk.user_id == self.user_id, KnowledgeChunk.embedder == embedder)
            .group_by(KnowledgeChunk.source_id)
        )
        return {r[0]: int(r[1]) for r in rows}

    async def set_vectors(self, vectors: list[tuple[int, list[float]]], embedder: str) -> None:
        for chunk_id, vec in vectors:
            await self.db.execute(
                text(
                    "UPDATE knowledge_chunks SET embedding = CAST(:v AS vector), embedder = :e "
                    "WHERE id = :id AND user_id = :uid"
                ),
                {"v": vector_literal(vec), "e": embedder, "id": chunk_id, "uid": self.user_id},
            )
        await self.db.commit()

    async def counts(self, source_id: uuid.UUID) -> tuple[int, int]:
        files = await self.db.scalar(
            select(func.count())
            .select_from(KnowledgeFile)
            .where(KnowledgeFile.source_id == source_id, KnowledgeFile.user_id == self.user_id)
        )
        chunks = await self.db.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.source_id == source_id, KnowledgeChunk.user_id == self.user_id)
        )
        return int(files or 0), int(chunks or 0)

    async def blocked_files(self, source_id: uuid.UUID) -> list[dict[str, Any]]:
        """Files with chunks the secret filter held back: path and rule names (never content)."""
        rows = await self.db.scalars(
            select(KnowledgeFile)
            .join(KnowledgeSource, KnowledgeSource.id == KnowledgeFile.source_id)
            .where(
                KnowledgeFile.source_id == source_id,
                self._visible(),
                func.jsonb_array_length(KnowledgeFile.blocked) > 0,
            )
            .order_by(KnowledgeFile.path)
        )
        return [
            {"path": f.path, "rules": sorted(set(f.blocked)), "chunks": len(f.blocked)}
            for f in rows
        ]

    async def blocked_overview(self) -> dict[str, Any]:
        """What the secret filter held back in the sources this user owns, for the status page:
        the number of chunks and, per file, its path, the rule names and the chunk count (never
        the content)."""
        rows = await self.db.execute(
            select(KnowledgeFile, KnowledgeSource.name)
            .join(KnowledgeSource, KnowledgeSource.id == KnowledgeFile.source_id)
            .where(
                KnowledgeSource.user_id == self.user_id,
                KnowledgeFile.user_id == self.user_id,
                func.jsonb_array_length(KnowledgeFile.blocked) > 0,
            )
            .order_by(KnowledgeSource.name, KnowledgeFile.path)
        )
        files = [
            {
                "source": name,
                "path": f.path,
                "rules": sorted(set(f.blocked)),
                "chunks": len(f.blocked),
            }
            for f, name in rows.all()
        ]
        return {"chunks": sum(f["chunks"] for f in files), "files": files}

    # --- search --------------------------------------------------------------------------

    async def nearest(
        self, vec: list[float], embedder: str, limit: int, source_id: uuid.UUID | None = None
    ) -> list[tuple[int, float]]:
        rows = await self.db.execute(
            text(
                "SELECT c.id, 1 - (c.embedding <=> CAST(:q AS vector)) AS sim "
                "FROM knowledge_chunks c JOIN knowledge_sources s ON s.id = c.source_id "
                "WHERE (s.user_id = :uid OR s.visibility = 'shared') AND c.embedder = :emb "
                "AND c.embedding IS NOT NULL AND (CAST(:sid AS uuid) IS NULL OR s.id = :sid) "
                "ORDER BY c.embedding <=> CAST(:q AS vector) LIMIT :n"
            ),
            {
                "q": vector_literal(vec),
                "uid": self.user_id,
                "emb": embedder,
                "sid": source_id,
                "n": limit,
            },
        )
        return [(r.id, float(r.sim)) for r in rows]

    async def keyword(
        self, query: str, limit: int, source_id: uuid.UUID | None = None
    ) -> list[tuple[int, float]]:
        words = keywords(query)
        if not words:
            return []
        tsq = " | ".join(f"{w}:*" for w in sorted(words))
        rows = await self.db.execute(
            text(
                "SELECT c.id, ts_rank_cd(to_tsvector('simple', c.heading || ' ' || c.text), "
                "to_tsquery('simple', :q)) AS r "
                "FROM knowledge_chunks c JOIN knowledge_sources s ON s.id = c.source_id "
                "WHERE (s.user_id = :uid OR s.visibility = 'shared') "
                "AND to_tsvector('simple', c.heading || ' ' || c.text) @@ to_tsquery('simple', :q) "
                "AND (CAST(:sid AS uuid) IS NULL OR s.id = :sid) ORDER BY r DESC LIMIT :n"
            ),
            {"q": tsq, "uid": self.user_id, "sid": source_id, "n": limit},
        )
        return [(r.id, float(r.r)) for r in rows]

    async def chunks(self, ids: Sequence[int]) -> dict[int, tuple[KnowledgeChunk, str]]:
        """The chunks (with their source's name) among ``ids`` that this user may see."""
        if not ids:
            return {}
        rows = await self.db.execute(
            select(KnowledgeChunk, KnowledgeSource.name)
            .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
            .where(KnowledgeChunk.id.in_(list(ids)), self._visible())
        )
        return {c.id: (c, name) for c, name in rows}

    async def chunks_of_path(self, path: str) -> list[tuple[KnowledgeChunk, str]]:
        """The chunks of the file at ``path`` that this user may see, in reading order, with the
        name of their source. Only one source is returned when several have a file of that path:
        the first by name, so one document is never read as two."""
        rows = await self.db.execute(
            select(KnowledgeChunk, KnowledgeSource.name)
            .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
            .where(KnowledgeChunk.path == path, self._visible())
            .order_by(KnowledgeSource.name, KnowledgeChunk.file_id, KnowledgeChunk.ord)
        )
        found = [(c, name) for c, name in rows]
        if not found:
            return []
        first = found[0][0].source_id
        return [(c, name) for c, name in found if c.source_id == first]
