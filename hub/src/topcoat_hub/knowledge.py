"""The knowledge base (E2-03 slim, D34): index folders of Markdown/text through a tool node,
search them, and give the agent a ``knowledge.search`` tool whose hits are ``[doc]`` evidence.

**Reading** goes only through the tool node's ``docs.list`` / ``docs.read`` (allow-listed roots,
the node's own exclude list, masking). The hub adds the same exclude list in front (a path that
matches it is not asked for, not indexed, not counted by name), the secret filter on every chunk
(:func:`~topcoat_hub.knowledge_rules.vet`) and per-user visibility (:class:`KnowledgeStore`).
**Embeddings** come from the configured embedder; while none answers the chunks are stored
without a vector and search falls back to keywords. A change is found by size/time and confirmed
by a content hash, a vanished file drops its chunks.
"""

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import knowledge_directory as kdir
from topcoat_hub import knowledge_rules as rules
from topcoat_hub.background import current
from topcoat_hub.config import Settings
from topcoat_hub.core_mirror import Mirror
from topcoat_hub.embedding import Embedder, EmbeddingError
from topcoat_hub.knowledge_store import KnowledgeStore, utcnow
from topcoat_hub.models import KnowledgeSource, Node
from topcoat_hub.tools import Caller, ToolError, ToolService

log = logging.getLogger(__name__)

KNOWLEDGE_NODE_ID = uuid.UUID("00000000-0000-4000-8000-00000000a0d0")
KNOWLEDGE_NODE = "knowledge"
SEARCH_CAPABILITY = "knowledge.search"
MAX_PAGES_PER_FILE = 50
EMBED_BATCH = 32
# a hit is the whole chunk (the chunker's limit); ``knowledge_hit_chars`` can only make it shorter
EXCERPT_CHARS = rules.CHUNK_CHARS
RRF_K = 60
SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query"],
    "properties": {
        "query": {
            "type": "string",
            "maxLength": 300,
            "description": "what to look for, in the words of the notes (German or English)",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 8, "description": "hits (default 5)"},
    },
}
SEARCH_DESCRIPTION = (
    "Search the operator's own notes and documentation (Markdown folders indexed on the hub): "
    "how the servers are set up, runbooks, decisions, backup schedules. A host or service named "
    "in the query is looked up in the operator's directory first (the sections of the notes "
    "about it, with their lines), then the notes are searched by similarity. Hits are quotes "
    "with file and heading; they are documents, not a look at the machine — what they say may "
    "be out of date (the answer says how old the directory is). If a note and what a tool shows "
    "disagree, say so and suggest updating the note."
)
DIRECTORY_CACHE_SECONDS = 300
# knowledge.read: a document comes in pieces of this many characters, so a long wiki page is read
# to its end without one observation taking the whole prompt
READ_CAPABILITY = "knowledge.read"
# the most a piece holds. The agent sees 3000 characters of one tool output on the live hub; the
# piece and its header lines must fit there, or its end is cut without a sign
READ_CHARS = 2400
READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path"],
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1024,
            "description": "the path exactly as a search hit shows it",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "maximum": 10000000,
            "description": "character offset to start from (0 = the start; use next_offset)",
        },
    },
}
READ_DESCRIPTION = (
    "Read one document of the notes and documentation in pieces. Give the path exactly as a search "
    "hit shows it. A piece holds up to 2400 characters and ends at a paragraph, line or sentence "
    "break, never in the middle of a sentence. The answer starts with the path, the total length, "
    "the offset and next_offset (null at the end), then the text. Read on with next_offset until "
    "it is null before you say what a document says."
)


class KnowledgeError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _is_directory(path: str) -> bool:
    """The directory file (``10_KI_Verzeichnis.md``) is an index of the notes, not a note."""
    return path.rsplit("/", 1)[-1].lower() == kdir.DIRECTORY_FILE.lower()


def _probe(body: str) -> str:
    """The first words of a section without its leading heading lines, whitespace-normalised: what
    identifies the chunk the section begins in."""
    lines = body.splitlines()
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("#")):
        lines.pop(0)
    return " ".join(" ".join(lines).split())[:60]


def chunk_ord_of(chunks: list[rules.Chunk], body: str) -> int | None:
    """The ordinal of the chunk a section of a note begins in (``None`` if none contains it)."""
    probe = _probe(body)
    if len(probe) < 12:
        return None
    for c in chunks:
        if probe in " ".join(c.text.split()):
            return c.ord
    return None


def read_tool() -> dict[str, Any]:
    """The virtual tool entry for reading one document in pieces (no node: it runs on the hub)."""
    return {
        "node_id": str(KNOWLEDGE_NODE_ID),
        "node": KNOWLEDGE_NODE,
        "capability": READ_CAPABILITY,
        "description": READ_DESCRIPTION,
        "input_schema": READ_SCHEMA,
    }


def _piece_end(text: str, offset: int) -> int:
    """Where the piece that starts at ``offset`` ends: at the last paragraph break, else the last
    line break, else the last sentence end within ``READ_CHARS``. A break in the first half of
    the window is not used (it would give a tiny piece). Only text without breaks is cut hard."""
    limit = offset + READ_CHARS
    if limit >= len(text):
        return len(text)
    window = text[offset:limit]
    for mark in ("\n\n", "\n", ". "):
        cut = window.rfind(mark)
        if cut >= READ_CHARS // 2:
            return offset + cut + len(mark)
    return limit


def read_page(text: str, offset: int) -> dict[str, Any]:
    """One piece of a document's text from ``offset``: the piece, the total length and where the
    next piece starts (``None`` when this piece reaches the end). The piece is ``content``, so the
    agent sees the header lines first and the text after them, unescaped."""
    end = _piece_end(text, offset) if offset < len(text) else len(text)
    return {
        "content": text[offset:end],
        "total_chars": len(text),
        "offset": offset,
        "next_offset": end if end < len(text) else None,
    }


def _with_root(args: dict[str, Any], root: str) -> dict[str, Any]:
    """``root`` empty = the node's only released directory (the node decides)."""
    return {**args, "root": root} if root else args


def search_tool() -> dict[str, Any]:
    """The virtual tool entry the agent is offered (no node: it runs on the hub)."""
    return {
        "node_id": str(KNOWLEDGE_NODE_ID),
        "node": KNOWLEDGE_NODE,
        "capability": SEARCH_CAPABILITY,
        "description": SEARCH_DESCRIPTION,
        "input_schema": SEARCH_SCHEMA,
    }


class KnowledgeService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        tools: ToolService,
        embedder: Embedder,
        settings: Settings,
    ) -> None:
        self._sm = sessionmaker
        self._tools = tools
        self._embedder = embedder
        self._settings = settings
        self._runs: dict[uuid.UUID, asyncio.Task[None]] = {}
        # the memory's mirror of the infrastructure profiles (E3-06); set by the app
        self.mirror: Mirror | None = None
        # the reports of the projects, written into the person's own source (project_knowledge.py);
        # set by the app, run as a step of the background task "knowledge"
        self.project_reports: Callable[[], Awaitable[int]] | None = None
        # parsed directories and note texts read for a lookup, by (source, path, mtime, size)
        self._dir_cache: dict[tuple[uuid.UUID, str, int, int], list[kdir.Entry]] = {}
        self._text_cache: dict[tuple[uuid.UUID, str, int, int], tuple[float, str]] = {}

    @property
    def extra_exclude(self) -> list[str]:
        return [g.strip() for g in self._settings.knowledge_exclude.split(",") if g.strip()]

    # --- sources ------------------------------------------------------------------------

    async def add_source(
        self, owner: uuid.UUID, name: str, node: str, root: str, visibility: str
    ) -> KnowledgeSource:
        """Create a source after asking the node once whether it releases that directory."""
        async with self._sm() as db:
            row = await db.scalar(select(Node).where(Node.name == node))
            if row is None:
                raise KnowledgeError(404, "unknown_node", f"no node named {node!r}")
            node_id, node_name = row.id, row.name
        try:
            first = await self._call(owner, node_id, "docs.list", _with_root({"limit": 1}, root))
        except KnowledgeError as exc:
            raise KnowledgeError(exc.status, exc.code, f"{node_name}: {exc.message}") from exc
        if not first.get("ok"):
            err = first.get("error") or {}
            raise KnowledgeError(
                422, "root_refused", f"{node_name}: {err.get('message') or 'docs.list failed'}"
            )
        async with self._sm() as db:
            source = await KnowledgeStore(db, owner).create_source(
                name=name, visibility=visibility, node_id=node_id, node_name=node_name, root=root
            )
        self.start(owner, source.id)
        return source

    # --- uploads (a source without a node) ------------------------------------------------

    async def put_upload(
        self,
        owner: uuid.UUID,
        source_id: uuid.UUID | None,
        name: str,
        visibility: str,
        filename: str,
        text: str,
    ) -> tuple[KnowledgeSource, dict[str, Any]]:
        """Add (or replace) one file of an upload source, creating the source when ``source_id``
        is empty. The same rules as for a folder: only notes, the exclude list, the secret
        filter."""
        path = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not path or not rules.wanted(path):
            raise KnowledgeError(
                422, "bad_file", "only Markdown or text files (.md, .markdown, .txt)"
            )
        if rules.excluded(path, self.extra_exclude):
            raise KnowledgeError(422, "excluded", "the name is on the exclude list")
        data = text.encode()
        if len(data) > rules.MAX_FILE_BYTES:
            raise KnowledgeError(413, "too_large", "the file is larger than 1 MiB")
        if "\x00" in text:
            raise KnowledgeError(422, "bad_file", "not a text file")
        async with self._sm() as db:
            store = KnowledgeStore(db, owner)
            if source_id is None:
                if not name:
                    raise KnowledgeError(422, "name_required", "a new upload source needs a name")
                source = await store.create_source(
                    name=name, visibility=visibility, kind="upload", node_name="", root=""
                )
            else:
                source = await store.own_source(source_id)
                if source is None:
                    raise KnowledgeError(404, "not_found", "unknown source")
                if source.kind != "upload":
                    raise KnowledgeError(409, "not_upload", "this source is read through a node")
            digest = hashlib.sha256(data).hexdigest()
            rows, blocked = await self._chunk_rows(path, text)
            await store.replace_file(
                source.id, path, len(data), int(time.time()), digest, blocked, rows
            )
            await self._recount(store, source.id)
            await db.refresh(source)
            return source, {"path": path, "chunks": len(rows), "blocked": len(blocked)}

    async def drop_upload(self, owner: uuid.UUID, source_id: uuid.UUID, path: str) -> bool:
        async with self._sm() as db:
            store = KnowledgeStore(db, owner)
            source = await store.own_source(source_id)
            if source is None or source.kind != "upload":
                return False
            if path not in await store.files(source_id):
                return False
            await store.drop_files(source_id, [path])
            await self._recount(store, source_id)
            return True

    async def _recount(self, store: KnowledgeStore, source_id: uuid.UUID) -> None:
        files, chunks = await store.counts(source_id)
        blocked = sum(len(f.blocked) for f in (await store.files(source_id)).values())
        await store.update_source(
            source_id,
            status="ready",
            error=None,
            file_count=files,
            chunk_count=chunks,
            blocked_count=blocked,
            indexed_at=utcnow(),
        )

    def start(self, owner: uuid.UUID, source_id: uuid.UUID) -> None:
        """Index (or re-index) a source in the background; one run per source at a time."""
        running = self._runs.get(source_id)
        if running is not None and not running.done():
            return
        self._runs[source_id] = asyncio.create_task(self._index(owner, source_id))

    async def wait(self, source_id: uuid.UUID) -> None:
        """Tests: wait for a source's run to finish."""
        run = self._runs.get(source_id)
        if run is not None:
            await run

    async def shutdown(self) -> None:
        for run in list(self._runs.values()):
            run.cancel()
        await asyncio.gather(*self._runs.values(), return_exceptions=True)

    async def reindex_all(self) -> int:
        """The periodic run: every source whose node is connected."""
        async with self._sm() as db:
            rows = (await db.scalars(select(KnowledgeSource))).all()
            pairs = [(s.user_id, s.id) for s in rows if s.status != "indexing"]
        for owner, source_id in pairs:
            self.start(owner, source_id)
        return len(pairs)

    async def scheduler(self) -> None:
        interval = self._settings.knowledge_reindex_seconds
        while True:
            await asyncio.sleep(interval)
            bg = current(self, "knowledge", self._settings)
            if not bg.on:
                continue
            if bg.step("reindex").on:
                try:
                    await self.reindex_all()
                except Exception:
                    log.exception("knowledge: the periodic re-index failed")
            if self.project_reports is not None and bg.step("project_reports").on:
                try:
                    await self.project_reports()
                except Exception:
                    log.exception("knowledge: the reports of the projects could not be indexed")

    # --- the node ----------------------------------------------------------------------

    async def _call(
        self, owner: uuid.UUID, node_id: uuid.UUID, capability: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            return await self._tools.invoke(
                Caller(owner, True, "knowledge"), node_id, capability, tool_input
            )
        except ToolError as exc:
            raise KnowledgeError(exc.status, exc.code, exc.message) from exc

    async def _list(
        self, owner: uuid.UUID, source: KnowledgeSource
    ) -> tuple[list[dict[str, Any]], int]:
        """Every file the node lists (paging), minus what the exclude list forbids."""
        files: list[dict[str, Any]] = []
        excluded = 0
        after = ""
        for _ in range(200):
            args = _with_root({"limit": 1500}, source.root)
            if after:
                args["after"] = after
            result = await self._call(owner, source.node_id, "docs.list", args)  # type: ignore[arg-type]
            if not result.get("ok"):
                err = result.get("error") or {}
                raise KnowledgeError(502, "list_failed", err.get("message") or "docs.list failed")
            out = result.get("output") or {}
            excluded += int(out.get("excluded") or 0)
            for f in out.get("files") or []:
                path = str(f.get("path", ""))
                if not path or not rules.wanted(path) or rules.excluded(path, self.extra_exclude):
                    excluded += 1  # the hub's own list: never asked for, never named
                    continue
                files.append(
                    {
                        "path": path,
                        "size": int(f.get("size") or 0),
                        "mtime": int(f.get("mtime") or 0),
                    }
                )
            if not out.get("truncated"):
                return files, excluded
            after = str(out.get("next_after") or "")
            if not after:
                return files, excluded
        return files, excluded

    async def _read(self, owner: uuid.UUID, source: KnowledgeSource, path: str) -> str:
        parts: list[str] = []
        offset = 0
        for _ in range(MAX_PAGES_PER_FILE):
            result = await self._call(
                owner,
                source.node_id,  # type: ignore[arg-type]
                "docs.read",
                _with_root({"path": path, "offset": offset}, source.root),
            )
            if not result.get("ok"):
                err = result.get("error") or {}
                raise KnowledgeError(502, "read_failed", err.get("message") or "docs.read failed")
            out = result.get("output") or {}
            parts.append(str(out.get("content") or ""))
            nxt = out.get("next_offset")
            if nxt is None:
                return "".join(parts)
            offset = int(nxt)
        return "".join(parts)

    # --- indexing ----------------------------------------------------------------------

    async def _embed(self, texts: list[str]) -> list[list[float]] | None:
        vectors: list[list[float]] = []
        try:
            for i in range(0, len(texts), EMBED_BATCH):
                vectors.extend(await self._embedder.embed(texts[i : i + EMBED_BATCH]))
        except EmbeddingError as exc:
            log.warning(
                "knowledge: no embeddings now, chunks are stored for keyword search: %s", exc
            )
            return None
        return vectors

    def _hit_chars(self) -> int:
        """How much of a hit is shown: the whole chunk, or less when the setting says so."""
        return max(int(self._settings.knowledge_hit_chars), 100)

    async def _index(self, owner: uuid.UUID, source_id: uuid.UUID) -> None:
        async with self._sm() as db:
            store = KnowledgeStore(db, owner)
            source = await store.own_source(source_id)
            if source is None:
                return
            await store.update_source(source_id, status="indexing", error=None)
            try:
                await self._run(owner, store, source)
            except KnowledgeError as exc:
                log.warning("knowledge: %s failed: %s", source.name, exc.message)
                await store.update_source(source_id, status="error", error=exc.message)
            except asyncio.CancelledError:
                await store.update_source(source_id, status="error", error="interrupted")
                raise
            except Exception:
                log.exception("knowledge: indexing %s crashed", source.name)
                await store.update_source(
                    source_id, status="error", error="unexpected error; see the hub log"
                )

    async def _run(self, owner: uuid.UUID, store: KnowledgeStore, source: KnowledgeSource) -> None:
        if source.kind == "upload":
            # nothing to read: only give chunks a vector of the current embedder (new model, outage)
            await self._reembed(store, source.id)
            await self._recount(store, source.id)
            return
        if source.node_id is None:
            raise KnowledgeError(409, "node_gone", "the node of this source was removed")
        listed, excluded = await self._list(owner, source)
        known = await store.files(source.id)
        await store.drop_files(
            source.id, [p for p in known if p not in {f["path"] for f in listed}]
        )
        blocked_total = 0
        mirrored = await self.mirror.known(owner, str(source.id)) if self.mirror else {}
        mirror_files: dict[str, str] = {}
        for f in listed:
            old = known.get(f["path"])
            is_directory = _is_directory(f["path"])
            profile = self.mirror is not None and rules.is_profile_path(f["path"])
            # a profile that is indexed but not (yet) mirrored is read again, once
            unmirrored = profile and (old is None or mirrored.get(f["path"]) != old.content_hash)
            # (an index made before the directory was kept out holds its chunks: rebuilt below)
            if (
                old
                and old.size == f["size"]
                and old.mtime == f["mtime"]
                and not (is_directory and old.chunk_count)
                and not unmirrored
                and not old.blocked  # held-back chunks are looked at again (the rules improve)
            ):
                blocked_total += len(old.blocked)
                continue
            text = await self._read(owner, source, f["path"])
            digest = hashlib.sha256(text.encode()).hexdigest()
            if profile:
                mirror_files[f["path"]] = text
            if (
                old
                and old.content_hash == digest
                and not (is_directory and old.chunk_count)
                and not old.blocked
            ):
                await store.touch_file(old, f["size"], f["mtime"])
                blocked_total += len(old.blocked)
                continue
            # the directory is an index of the notes, not a note: its lines would only be noise
            rows, blocked = await self._chunk_rows(f["path"], text, skip=is_directory)
            blocked_total += len(blocked)
            await store.replace_file(
                source.id, f["path"], f["size"], f["mtime"], digest, blocked, rows
            )
        await self._mirror(owner, source, mirror_files, mirrored, {f["path"] for f in listed})
        await self._reembed(store, source.id)
        files, chunks = await store.counts(source.id)
        await store.update_source(
            source.id,
            status="ready",
            error=None,
            file_count=files,
            chunk_count=chunks,
            excluded_count=excluded,
            blocked_count=blocked_total,
            indexed_at=utcnow(),
        )

    async def _chunk_rows(
        self, path: str, text: str, skip: bool = False
    ) -> tuple[list[tuple[int, str, str, list[float] | None, str | None]], list[str]]:
        """The chunks of one file that pass the secret filter, with their vectors (``None`` while
        no embedder answers), and the names of the rules that held chunks back."""
        keep: list[rules.Chunk] = []
        blocked: list[str] = []
        for chunk in [] if skip else rules.chunk_file(path, text):
            body, removed = rules.vet_lines(chunk.text)
            if removed:
                blocked.extend(removed)
                log.warning(
                    "knowledge: %s: %s (%s), the secret filter rule(s) %s matched",
                    path,
                    "lines of a chunk were left out"
                    if body is not None
                    else "a chunk was not indexed",
                    chunk.heading,
                    ", ".join(removed),
                )
            if body is None:
                continue
            keep.append(rules.Chunk(chunk.heading, body, len(keep)))
        vectors = await self._embed([f"{c.heading}\n{c.text}" for c in keep]) if keep else []
        rows = [
            (
                c.ord,
                c.heading,
                c.text,
                vectors[i] if vectors is not None else None,
                self._embedder.id if vectors is not None else None,
            )
            for i, c in enumerate(keep)
        ]
        return rows, sorted(blocked)

    async def _mirror(
        self,
        owner: uuid.UUID,
        source: KnowledgeSource,
        files: dict[str, str],
        mirrored: dict[str, str],
        listed: set[str],
    ) -> None:
        """The profiles that are new, changed or gone go to the memory (never fails the run)."""
        if self.mirror is None:
            return
        vanished = [p for p in mirrored if p not in listed]
        if not files and not vanished:
            return
        try:
            stats = await self.mirror.apply(owner, str(source.id), files, vanished)
            log.info("knowledge: %s: profiles mirrored %s", source.name, stats)
        except Exception:
            log.exception("knowledge: mirroring the profiles of %s failed", source.name)

    async def _reembed(self, store: KnowledgeStore, source_id: uuid.UUID) -> None:
        """Vectors for chunks that have none of the current embedder (new model, earlier outage)."""
        todo = await store.unembedded(source_id, self._embedder.id)
        if not todo:
            return
        vectors = await self._embed([f"{h}\n{t}" for _, h, t in todo])
        if vectors is not None:
            await store.set_vectors(
                [(c[0], v) for c, v in zip(todo, vectors, strict=True)], self._embedder.id
            )

    # --- the directory -----------------------------------------------------------------

    async def _note_text(self, source: KnowledgeSource, path: str, size: int, mtime: int) -> str:
        """A note as the node has it now; kept for a few minutes (a lookup reads the same file
        for several sections)."""
        key = (source.id, path, mtime, size)
        hit = self._text_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < DIRECTORY_CACHE_SECONDS:
            return hit[1]
        text = await self._read(source.user_id, source, path)
        if len(self._text_cache) > 64:
            self._text_cache.clear()
        self._text_cache[key] = (time.monotonic(), text)
        return text

    async def _directory(
        self, source: KnowledgeSource, path: str, size: int, mtime: int
    ) -> list[kdir.Entry]:
        key = (source.id, path, mtime, size)
        if key not in self._dir_cache:
            text = await self._note_text(source, path, size, mtime)
            if len(self._dir_cache) > 16:
                self._dir_cache.clear()
            self._dir_cache[key] = kdir.parse(text)
        return self._dir_cache[key]

    async def lookup(
        self, user_id: uuid.UUID, query: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """The directory first (E3-04): the hosts and services the query names, looked up in
        ``10_KI_Verzeichnis.md`` of the sources this user may see, and exactly the sections it
        lists for them — read from the node, never a file the node did not list, each section
        through the secret filter. Returns the hits and what is known about the directory (its
        file, its age, which entries matched); ``None`` when there is no directory."""
        async with self._sm() as db:
            store = KnowledgeStore(db, user_id)
            sources = [s for s in await store.sources() if s.node_id is not None]
            listings = {s.id: await store.files(s.id) for s in sources}
        hits: list[dict[str, Any]] = []
        meta: dict[str, Any] | None = None
        withheld: list[dict[str, str]] = []  # sections the secret filter kept back (path, rule)
        shown: set[tuple[str, int | None, int | None, str]] = set()
        stale_days = self._settings.knowledge_directory_stale_days
        for source in sources:
            files = listings[source.id]
            dpath = min(
                (p for p in files if p.rsplit("/", 1)[-1].lower() == kdir.DIRECTORY_FILE.lower()),
                key=len,
                default=None,
            )
            if dpath is None:
                continue
            dfile = files[dpath]
            try:
                entries = await self._directory(source, dpath, dfile.size, dfile.mtime)
            except KnowledgeError as exc:
                log.info(
                    "knowledge: the directory of %s is not readable: %s", source.name, exc.message
                )
                continue
            matched = kdir.match(entries, query)
            age = max((datetime.now(UTC) - datetime.fromtimestamp(dfile.mtime, UTC)).days, 0)
            info: dict[str, Any] = {
                "file": dpath,
                "source": source.name,
                "age_days": age,
                "entries": len(entries),
                "matched": [e.name for e in matched],
            }
            if age > stale_days:
                info["note"] = (
                    f"the directory is {age} days old: sections written since are not listed in it"
                )
            meta = meta or info
            for entry in matched:
                for ref in entry.refs:
                    if len(hits) >= kdir.MAX_REFS:
                        break
                    note = files.get(ref.path)
                    # only what the node listed (allowed, not excluded) and the hub's list allows
                    if note is None or rules.excluded(ref.path, self.extra_exclude):
                        continue
                    try:
                        text = await self._note_text(source, ref.path, note.size, note.mtime)
                    except KnowledgeError:
                        continue
                    heading, body = kdir.section(text, ref, self._hit_chars())
                    clean, rule = rules.vet(body) if body else (None, None)
                    if not clean:
                        if body:  # a secret in it: not shown, but not silent either
                            withheld.append({"path": ref.path, "rule": rule or "secret"})
                            log.warning(
                                "knowledge: a section of %s (via the directory, %r) is withheld, "
                                "the secret filter rule %r matched",
                                ref.path,
                                entry.name,
                                rule or "secret",
                            )
                        continue  # empty, or a secret in it
                    chunks = rules.chunk_file(ref.path, text)
                    ord_ = chunk_ord_of(chunks, body)
                    # the same section named by two entries (a host and its service) is shown once
                    key = (
                        (ref.path, ref.start, ref.end, heading)
                        if ord_ is None
                        else (
                            ref.path,
                            ord_,
                            None,
                            "",
                        )
                    )
                    if key in shown:
                        continue
                    shown.add(key)
                    # a reference without lines points at the start of the note: the heading is
                    # the note's first one (never the host's name, which no chunk carries)
                    first = chunks[0].heading if chunks else ref.path.rsplit("/", 1)[-1]
                    hits.append(
                        {
                            "source": source.name,
                            "path": ref.path,
                            "heading": heading or ref.label or first,
                            "ord": ord_,
                            "text": clean,
                            "mtime": note.mtime,
                            "via": "directory",
                            "entity": entry.name,
                            **(
                                {"lines": f"{ref.start}-{ref.end}" if ref.end else str(ref.start)}
                                if ref.start
                                else {}
                            ),
                        }
                    )
        if withheld:
            meta = {**(meta or {}), "withheld": withheld}
        return hits, meta

    # --- search ------------------------------------------------------------------------

    async def search(
        self, user_id: uuid.UUID, query: str, limit: int = 5, source_id: uuid.UUID | None = None
    ) -> list[dict[str, Any]]:
        """Hybrid search over what this user may see: vector and keyword hits, rank-fused."""
        return (await self.search_full(user_id, query, limit, source_id))[0]

    async def search_full(
        self, user_id: uuid.UUID, query: str, limit: int = 5, source_id: uuid.UUID | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """:meth:`search` and what is known about the directory: the sections the directory lists
        for a host or service in the query come first, the similarity search fills the rest."""
        query = query.strip()
        if not query:
            return [], None
        directory_hits: list[dict[str, Any]] = []
        directory: dict[str, Any] | None = None
        if source_id is None:
            try:
                directory_hits, directory = await self.lookup(user_id, query)
            except Exception:
                log.exception("knowledge: the directory lookup failed")
        directory_hits = directory_hits[:limit]
        if len(directory_hits) >= limit:
            return directory_hits, directory
        seen = {(h["path"], h["heading"]) for h in directory_hits}
        # the same section can come from the directory (with its own heading) and from the search
        # (the chunk's heading path): the chunk ordinal of the file tells them apart
        seen_ord = {(h["path"], h["ord"]) for h in directory_hits if h.get("ord") is not None}
        rest = await self._similar(user_id, query, limit, source_id)
        merged = directory_hits + [
            h
            for h in rest
            if (h["path"], h["heading"]) not in seen and (h["path"], h["ord"]) not in seen_ord
        ]
        return merged[:limit], directory

    async def _similar(
        self, user_id: uuid.UUID, query: str, limit: int, source_id: uuid.UUID | None
    ) -> list[dict[str, Any]]:
        wide = max(limit * 4, 12)
        async with self._sm() as db:
            store = KnowledgeStore(db, user_id)
            ranked: dict[int, float] = {}
            transitional = self._embedder.id.startswith("hashing")
            # the hashing embedder matches words, so the filler words of a question only add
            # noise; a real embedding model reads the whole question
            ask = " ".join(rules.keywords(query)) or query if transitional else query
            try:
                (vec,) = await self._embedder.embed([ask])
            except EmbeddingError:
                vec = None  # keywords only
            if vec is not None:
                floor = (
                    max(self._embedder.recall_min, 0.3)
                    if transitional
                    else self._embedder.recall_min
                )
                near = await store.nearest(vec, self._embedder.id, wide, source_id)
                near = [(i, s) for i, s in near if s >= floor]
                for rank, (cid, _sim) in enumerate(near):
                    ranked[cid] = ranked.get(cid, 0.0) + 1 / (RRF_K + rank)
            for rank, (cid, _r) in enumerate(await store.keyword(query, wide, source_id)):
                ranked[cid] = ranked.get(cid, 0.0) + 1 / (RRF_K + rank)
            order = sorted(ranked, key=lambda c: -ranked[c])
            found = await store.chunks(order)
        hits: list[dict[str, Any]] = []
        for cid in order:
            if cid not in found:
                continue
            chunk, source_name = found[cid]
            if rules.excluded(chunk.path, self.extra_exclude):
                continue  # the list grew after indexing: the hit is dropped, not named
            if _is_directory(chunk.path):
                continue  # an old index may still hold chunks of the directory file
            cap = self._hit_chars()
            body = chunk.text if len(chunk.text) <= cap else chunk.text[:cap].rstrip() + " …"
            hits.append(
                {
                    "source": source_name,
                    "path": chunk.path,
                    "heading": chunk.heading,
                    "text": body,
                    "ord": chunk.ord,
                    "mtime": chunk.mtime,
                    "score": round(ranked[cid], 4),
                    "via": "search",
                }
            )
            if len(hits) >= limit:
                break
        return hits

    # --- the agent's tool ---------------------------------------------------------------

    async def offer(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        """The virtual tool, if this user has anything to search."""
        async with self._sm() as db:
            if await KnowledgeStore(db, user_id).any_visible_chunk():
                return [search_tool(), read_tool()]
        return []

    async def read(self, user_id: uuid.UUID, tool_input: dict[str, Any]) -> dict[str, Any]:
        """``knowledge.read`` as the agent loop calls it: one piece of one document."""
        unknown = set(tool_input) - {"path", "offset"}
        if unknown:
            raise ToolError(422, "invalid_input", f"unknown input: {', '.join(sorted(unknown))}")
        path = tool_input.get("path")
        offset = tool_input.get("offset", 0)
        if not isinstance(path, str) or not path.strip() or len(path) > 1024:
            raise ToolError(422, "invalid_input", "path must be a text of 1..1024 characters")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ToolError(422, "invalid_input", "offset must be a whole number, 0 or more")
        async with self._sm() as db:
            store = KnowledgeStore(db, user_id)
            found = await store.chunks_of_path(path)
            if not found and "/" in path:
                # a search hit names the source (its own field) and the path; a caller that joins
                # them ("Akten/x.md") means the same document as "x.md"
                path = path.split("/", 1)[1]
                found = await store.chunks_of_path(path)
        if not found:
            raise ToolError(
                404,
                "not_found",
                "no document has this path: use the path a search hit shows (its source is a "
                "separate field, not part of the path)",
            )
        text = "\n\n".join(c.text for c, _ in found)
        output = {"path": path, "source": found[0][1], **read_page(text, offset)}
        # the same envelope as a tool node's answer: the agent loop reads ``ok`` first
        return {"ok": True, "output": output, "at": utcnow().isoformat(), "bytes": len(str(output))}

    async def run_tool(self, user_id: uuid.UUID, tool_input: dict[str, Any]) -> dict[str, Any]:
        """``knowledge.search`` as the agent loop calls it: the shape of a tool node's answer."""
        query = tool_input.get("query")
        limit = tool_input.get("limit", 5)
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            raise ToolError(422, "invalid_input", "query must be a text of 1..300 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 8:
            raise ToolError(422, "invalid_input", "limit must be 1..8")
        unknown = set(tool_input) - {"query", "limit"}
        if unknown:
            raise ToolError(422, "invalid_input", f"unknown input: {', '.join(sorted(unknown))}")
        hits, directory = await self.search_full(user_id, query, limit)
        output: dict[str, Any] = {"query": query, "hits": hits, "count": len(hits)}
        if directory is not None:
            output["directory"] = directory
        return {"ok": True, "output": output, "at": utcnow().isoformat(), "bytes": len(str(output))}
