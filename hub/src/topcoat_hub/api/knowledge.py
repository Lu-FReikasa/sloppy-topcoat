"""The knowledge base over REST (E2-03, D34): sources (admin), search (everyone).

A source is visible to its owner and, when ``shared``, to every user; ``private`` ones only to
their owner (D20). Creating, re-indexing and deleting a source is for the admin (dev token) —
an API key never does it, like every other admin action.
"""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.auth import CurrentUser, api_user, require_admin
from topcoat_hub.db import get_db
from topcoat_hub.knowledge import KnowledgeError, KnowledgeService
from topcoat_hub.knowledge_rules import DEFAULT_EXCLUDE
from topcoat_hub.knowledge_store import KnowledgeStore
from topcoat_hub.models import KnowledgeSource

router = APIRouter(prefix="/api/knowledge")


def _service(request: Request) -> KnowledgeService:
    return request.app.state.knowledge


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    kind: Literal["node"] = "node"
    node: str = Field("", max_length=63, description="name of the tool node")
    root: str = Field(
        "",
        max_length=512,
        description="a directory the node releases; empty = its only released directory",
    )
    visibility: Literal["shared", "private"] = "shared"


class UploadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: uuid.UUID | None = Field(None, description="an upload source; empty = create one")
    name: str = Field("", max_length=100, description="name of the new source")
    visibility: Literal["shared", "private"] = "shared"
    filename: str = Field(min_length=1, max_length=255)
    text: str = Field(max_length=2_000_000)


def source_view(
    s: KnowledgeSource, viewer: uuid.UUID, embedded: int | None = None
) -> dict[str, Any]:
    """``embedded``: chunks with a vector of the embedder in use; only the owner's sources have
    it (``None`` elsewhere and where the caller did not count)."""
    return {
        "id": str(s.id),
        "name": s.name,
        "visibility": s.visibility,
        "kind": s.kind,
        "own": s.user_id == viewer,
        "node": s.node_name,
        "root": s.root
        if s.user_id == viewer
        else None,  # "" = the node's only root  # where the files are is the owner's
        "status": s.status,
        "error": s.error if s.user_id == viewer else None,
        "files": s.file_count,
        "chunks": s.chunk_count,
        "embedded": embedded if s.user_id == viewer else None,
        "excluded": s.excluded_count,
        "blocked": s.blocked_count,
        "indexed_at": _iso(s.indexed_at),
        "created_at": _iso(s.created_at),
    }


@router.get("/sources")
async def list_sources(
    request: Request, db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(api_user)
) -> list[dict[str, Any]]:
    store = KnowledgeStore(db, user.uuid)
    embedded = await store.embedded_counts(request.app.state.embedder.id)
    return [source_view(s, user.uuid, embedded.get(s.id, 0)) for s in await store.sources()]


@router.post("/sources", status_code=201)
async def create_source(
    body: SourceCreate, request: Request, admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        if not body.node.strip():
            raise KnowledgeError(422, "node_required", "a folder source needs a node")
        source = await _service(request).add_source(
            admin.uuid, body.name.strip(), body.node.strip(), body.root.strip(), body.visibility
        )
    except KnowledgeError as exc:
        raise _error(exc.status, exc.code, exc.message) from exc
    return source_view(source, admin.uuid)


@router.post("/uploads", status_code=201)
async def upload(
    body: UploadBody, request: Request, admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    """Put one Markdown/text file into an upload source (a source without a node), creating the
    source when ``source_id`` is empty. Same rules as a folder: exclude list, secret filter."""
    try:
        source, file = await _service(request).put_upload(
            admin.uuid,
            body.source_id,
            body.name.strip(),
            body.visibility,
            body.filename,
            body.text,
        )
    except KnowledgeError as exc:
        raise _error(exc.status, exc.code, exc.message) from exc
    return {**source_view(source, admin.uuid), "file": file}


@router.delete("/sources/{source_id}/files", status_code=204)
async def delete_upload_file(
    source_id: uuid.UUID,
    request: Request,
    path: str = Query(min_length=1, max_length=1024),
    admin: CurrentUser = Depends(require_admin),
) -> None:
    if not await _service(request).drop_upload(admin.uuid, source_id, path):
        raise _error(404, "not_found", "unknown upload file")


@router.get("/sources/{source_id}")
async def get_source(
    source_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    store = KnowledgeStore(db, user.uuid)
    source = await store.get_source(source_id)
    if source is None:
        raise _error(404, "not_found", "unknown source")
    view = source_view(source, user.uuid)
    if source.user_id == user.uuid:
        view["blocked_files"] = await store.blocked_files(source_id)
    return view


@router.post("/sources/{source_id}/reindex")
async def reindex(
    source_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    source = await KnowledgeStore(db, admin.uuid).own_source(source_id)
    if source is None:
        raise _error(404, "not_found", "unknown source")
    if source.status == "indexing":
        raise _error(409, "indexing", "the source is being indexed right now")
    # say so before answering: the run itself starts a moment later, and whoever looks at the status
    # right after this call (the page, a script) must not still read the previous "ready"
    await KnowledgeStore(db, admin.uuid).update_source(source_id, status="indexing", error=None)
    _service(request).start(admin.uuid, source_id)
    await db.refresh(source)
    return source_view(source, admin.uuid)


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: CurrentUser = Depends(require_admin),
) -> None:
    if not await KnowledgeStore(db, admin.uuid).delete_source(source_id):
        raise _error(404, "not_found", "unknown source")


@router.get("/search")
async def search(
    request: Request,
    q: str = Query(min_length=1, max_length=300),
    limit: int = Query(5, ge=1, le=20),
    source_id: uuid.UUID | None = None,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """Hybrid search (keywords and vectors, rank-fused) over what the caller may see. A host or
    service named in ``q`` is looked up in the directory first; ``directory`` says which file that
    was, how old it is and which entries matched (absent without a directory)."""
    hits, directory = await _service(request).search_full(user.uuid, q, limit, source_id)
    body: dict[str, Any] = {"query": q, "hits": hits}
    if directory is not None:
        body["directory"] = directory
    return body


@router.get("/status")
async def status(
    request: Request, db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The embedder, the exclude list and what the secret filter held back: ``blocked.chunks`` and,
    per file of the caller's own sources, ``source``, ``path``, ``rules`` and ``chunks`` — so a
    note that is missing from the search can be traced to the rule that kept it out."""
    service = _service(request)
    embedder = request.app.state.embedder
    return {
        "embedder": embedder.id,
        "transitional": embedder.id.startswith("hashing"),
        "exclude": [*DEFAULT_EXCLUDE, *service.extra_exclude],
        "hit_chars": service._hit_chars(),
        "blocked": await KnowledgeStore(db, user.uuid).blocked_overview(),
    }
