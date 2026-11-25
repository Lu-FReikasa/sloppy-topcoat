"""Memory API (E2-02): the page where the user sees, corrects and deletes what the assistant
remembers. Every call works on the caller's own notes (:class:`MemoryStore`, D20); another
user's id answers 404. Nothing is deleted silently: changes create versions with a reason."""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import memory_care, memory_filter, memory_scope, memory_tags
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.chat_store import ChatStore
from topcoat_hub.gateway import spawn
from topcoat_hub.memory import MemoryService
from topcoat_hub.memory_store import (
    _UNSET,
    EVIDENCE,
    FACT_KINDS,
    MemoryStore,
    PlacementError,
    normalise_text,
)
from topcoat_hub.memory_tags import TagError, TagTree
from topcoat_hub.models import MemoryItem, MemoryQuestion, MemoryReport, MemoryRun, MemoryVersion

router = APIRouter(prefix="/api")


def _http(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt else None


def item_view(
    i: MemoryItem, cfg: memory_care.CareConfig | None = None, tags: list[str] | None = None
) -> dict[str, Any]:
    cfg = cfg or memory_care.DEFAULT
    now = memory_care.utcnow()
    return {
        "kind": i.kind,
        "stage": memory_care.stage(i, now, cfg),
        "reliability": memory_care.reliability(i, now, cfg),
        "usefulness": memory_care.usefulness(i, now),
        "last_verified_at": _iso(i.last_verified_at),
        "verified_count": i.verified_count,
        "expires_at": _iso(i.expires_at),
        "verify_with": i.verify_with,
        "recalled_count": i.recalled_count,
        "used_count": i.used_count,
        "last_used_at": _iso(i.last_used_at),
        "feedback_positive": i.feedback_positive,
        "feedback_negative": i.feedback_negative,
        "halflife_days": round(memory_care.halflife_days(i, cfg), 1),
        "core": i.core,
        "scope": i.scope,
        "application": i.application,
        "tags": tags or [],
        "example": i.example,
        "weak": i.weak,
        "id": str(i.id),
        "text": i.text,
        "status": i.status,
        "evidence": i.evidence,
        "confidence": i.confidence,
        "source_type": i.source_type,
        "source_ref": i.source_ref or {},
        "version": i.version,
        "confirmed_count": i.confirmed_count,
        "last_confirmed_at": _iso(i.last_confirmed_at),
        "disputed_with": str(i.disputed_with) if i.disputed_with else None,
        "superseded_by": str(i.superseded_by) if i.superseded_by else None,
        "embedder": i.embedder,
        "created_at": _iso(i.created_at),
        "updated_at": _iso(i.updated_at),
    }


def version_view(v: MemoryVersion) -> dict[str, Any]:
    return {
        "version": v.version,
        "source": v.source or {},
        "text": v.text,
        "status": v.status,
        "evidence": v.evidence,
        "confidence": v.confidence,
        "reason": v.reason,
        "actor": v.actor,
        "created_at": _iso(v.created_at),
    }


def question_view(q: MemoryQuestion) -> dict[str, Any]:
    return {
        "id": str(q.id),
        "topic": q.topic,
        "origin": q.origin,
        "status": q.status,
        "candidates": (q.context or {}).get("candidates", []),
        "question": (q.context or {}).get("question"),  # the concrete point, when it was named
        "observed": (q.context or {}).get("observed"),  # what a care check saw (origin "care")
        # D74: a question of the cross-check is shown where the decision lies (a program, or
        # "person" for Settings); null = wherever its notes are
        "program": (q.context or {}).get("program"),
        "resolution": q.resolution,
        "created_at": _iso(q.created_at),
        "answered_at": _iso(q.answered_at),
    }


def run_view(r: MemoryRun) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "kind": r.kind,
        "trigger": r.trigger,
        "status": r.status,
        "stats": r.stats or {},
        "started_at": _iso(r.started_at),
        "finished_at": _iso(r.finished_at),
    }


def care_settings(s: Any) -> dict[str, Any]:
    """The numbers the care works with (read-only on the page; they are ``TOPCOAT_MEMORY_*``)."""
    return {
        "hour": s.memory_care_hour,
        "interval_seconds": s.memory_care_interval_seconds,
        "halflife_days": {
            "person": s.memory_halflife_person_days,
            "preference": s.memory_halflife_preference_days,
            "operational": s.memory_halflife_operational_days,
            "volatile": s.memory_halflife_volatile_days,
        },
        "volatile_ttl_hours": s.memory_volatile_ttl_hours,
        "check_threshold": s.memory_check_threshold,
        "archive": {
            "usefulness": s.memory_archive_usefulness,
            "reliability": s.memory_archive_reliability,
            "idle_days": s.memory_archive_idle_days,
        },
        "max_notes": s.memory_care_max_notes,
    }


def _service(request: Request) -> MemoryService:
    return request.app.state.memory  # type: ignore[no-any-return]


def _cfg(request: Request) -> memory_care.CareConfig:
    return _service(request).care_config


async def _db(request: Request) -> Any:
    async with request.app.state.sessionmaker() as db:
        yield db


Db = Annotated[AsyncSession, Depends(_db)]


class ItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(None, min_length=1, max_length=1000)
    evidence: Literal["live", "doc", "assumption"] | None = None


class RevertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class AnswerBody(BaseModel):
    """The user's answer to a clarification question: this note is right, none of them is
    (optionally with what is true), or a statement."""

    model_config = ConfigDict(extra="forbid")

    note_id: uuid.UUID | None = None
    none: bool = False
    text: str | None = Field(None, max_length=300)


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep: Literal["this", "other", "both"]


@router.get("/memory")
async def list_memory(
    db: Db,
    request: Request,
    user: CurrentUser = Depends(api_user),
    q: str | None = None,
    status: Annotated[
        str | None, Query(pattern="^(active|superseded|disputed|deleted|archived)$")
    ] = None,
    evidence: Annotated[str | None, Query(pattern="^(live|doc|assumption)$")] = None,
    kind: Annotated[str | None, Query(pattern="^(person|preference|operational)$")] = None,
    application: Annotated[
        str | None, Query(pattern="^(core|shared|[a-z0-9][a-z0-9-]*)$", max_length=40)
    ] = None,
    tag: Annotated[str | None, Query(max_length=30)] = None,
) -> Any:
    store = MemoryStore(db, user.uuid)
    memory = _service(request)
    settings = await ChatStore(db, user.uuid).get_settings()
    # a program's page shows what it knows, "core" (formerly "shared") is what every program reads
    core = application in ("core", "shared")
    wanted: Any = _UNSET if application is None else (None if core else application)
    kinds = FACT_KINDS if application is not None and not core else None
    await store.tree().ensure()
    found = await store.items(
        q, status, evidence, kind=kind, kinds=kinds, application=wanted, tag=tag
    )
    names = await store.tag_names(found)
    await db.commit()
    return {
        "items": [item_view(i, _cfg(request), names.get(i.id)) for i in found],
        "applications": await store.counts_by_application(),
        "counts": await store.counts(),
        "blocked": await store.blocked_count(),
        "enabled": settings.memory_enabled if settings else True,
        "embedder": {
            "id": memory.embedder.id,
            "transitional": memory.embedder.id.startswith("hashing"),
        },
        "questions": [question_view(q) for q in await store.questions("open")],
        "kinds": memory_care.kinds(await store.live()),
        "running": await store.running_run("consolidation") is not None,
        "runs": [run_view(r) for r in await store.runs(5, "consolidation")],
        "reviews": {"runs": [run_view(r) for r in await store.runs(10, "review")]},
        "digest": request.app.state.digester.status(user.uuid),
        "care": {
            "running": await store.running_run("care") is not None,
            "runs": [run_view(r) for r in await store.runs(10, "care")],
            "settings": care_settings(request.app.state.settings),
        },
    }


@router.get("/conversations/{conversation_id}/memory")
async def conversation_memory(
    conversation_id: uuid.UUID, request: Request, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    """Notes remembered from one conversation (for the "remembered: …" lines after a reload)."""
    store = MemoryStore(db, user.uuid)
    found = await store.items(conversation_id=conversation_id)
    names = await store.tag_names(found)
    return [item_view(i, _cfg(request), names.get(i.id)) for i in found]


@router.get("/memory/questions")
async def open_questions(db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    """The clarification questions waiting for the caller (nobody else's, D20)."""
    return [question_view(q) for q in await MemoryStore(db, user.uuid).questions("open")]


@router.post("/memory/questions/{question_id}/answer")
async def answer_question(
    question_id: uuid.UUID,
    body: AnswerBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> Any:
    """Answer a question. The answer is an explicit statement of the user and is applied like
    one: the notes it rules out are replaced (or withdrawn), with the answer as the reason."""
    try:
        return await _service(request).apply_answer(
            user.uuid,
            question_id,
            {
                "note_id": str(body.note_id) if body.note_id else None,
                "none": body.none,
                "text": body.text,
            },
        )
    except LookupError:
        raise _http(404, "not_found", "unknown or already answered question") from None
    except ValueError as exc:
        code = str(exc)
        messages = {
            "secret_blocked": "This text looks like a secret and is not stored.",
            "unknown_note": "That note is not part of the question.",
            "empty_answer": "Choose a note, 'none of them' or write what is true.",
        }
        raise _http(422, code, messages.get(code, "invalid answer")) from None


@router.post("/memory/questions/{question_id}/dismiss")
async def dismiss_question(
    question_id: uuid.UUID, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    """Not now: the notes stay disputed; the question is asked again only after they change."""
    store = MemoryStore(db, user.uuid)
    closed = await store.close_question(question_id, "dismissed", "postponed by the user")
    if closed is None:
        raise _http(404, "not_found", "unknown question")
    await db.commit()
    return question_view(closed)


async def _own(store: MemoryStore, item_id: uuid.UUID) -> MemoryItem:
    item = await store.get(item_id)
    if item is None:
        raise _http(404, "not_found", "unknown memory item")
    return item


class PlacementBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # a key moves the note to that program, null puts it into the core; absent leaves it
    application: str | None = Field(None, max_length=40)
    # tag names; "parent/name" makes a new tag below a known one
    tags: list[str] | None = Field(None, max_length=memory_tags.MAX_TAGS_PER_NOTE)


@router.patch("/memory/{item_id}/placement")
async def place_memory_item(
    item_id: uuid.UUID,
    body: PlacementBody,
    request: Request,
    db: Db,
    user: CurrentUser = Depends(api_user),
) -> Any:
    """Move a note to a program (or into the core) and set its tags: where a note is seen and
    what it is about, not what it says."""
    store = MemoryStore(db, user.uuid)
    kwargs: dict[str, Any] = {}
    if "application" in body.model_fields_set:
        key = memory_scope.clean_application(body.application)
        if body.application not in (None, "") and key is None:
            raise _http(422, "application", "a program is a key such as dev, or null")
        kwargs["application"] = key
    if body.tags is not None:
        kwargs["tags"] = body.tags
    try:
        item = await store.set_placement(item_id, **kwargs)
    except PlacementError as exc:
        raise _http(422, "program_kind", str(exc)) from None
    if item is None:
        raise _http(404, "not_found", "no such note")
    names = await store.tag_names([item])
    await db.commit()
    return item_view(item, _cfg(request), names.get(item.id))


# --- the cross-check of the memories (D74) -------------------------------------------------------


@router.get("/memory/reports")
async def memory_reports(
    request: Request,
    db: Db,
    user: CurrentUser = Depends(api_user),
    recipient: Annotated[str, Query(pattern="^[a-z0-9][a-z0-9-]*$", max_length=40)] = "person",
) -> Any:
    """What a program (or the person, ``person``) was told by the cross-check: the discrepancies
    of its topics — both notes, it is responsible for them — and the notices other programs sent
    about a note of its own. Another program's report is never in it."""
    check = request.app.state.memory_care.crosscheck
    reports = await check.reports(db, user.uuid, recipient)
    store = MemoryStore(db, user.uuid)
    ids = [i for r in reports for i in (r.item_a, r.item_b) if i is not None]
    notes = {n.id: n for n in await store.get_many(ids)}

    def note(item_id: uuid.UUID | None) -> dict[str, Any] | None:
        n = notes.get(item_id) if item_id else None
        if n is None:
            return None
        return {
            "id": str(n.id),
            "text": n.text,
            "status": n.status,
            "application": n.application,
        }

    return {
        "reports": [
            {
                "id": str(r.id),
                "kind": r.kind,
                "recipient": r.recipient,
                "sender": r.sender,
                "topic": r.topic,
                "status": r.status,
                "reason": r.reason,
                "message": r.message,
                "decision": r.decision or {},
                "created_at": _iso(r.created_at),
                "decided_at": _iso(r.decided_at),
                # a notice says what the sender chose to say, and names the note of the recipient
                "a": note(r.item_a),
                "b": note(r.item_b) if r.kind == "discrepancy" else None,
            }
            for r in reports
        ]
    }


@router.post("/memory/reports/{report_id}/seen")
async def report_seen(report_id: uuid.UUID, db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    """The person read a notice: it is put away."""

    report = await db.scalar(
        select(MemoryReport).where(MemoryReport.id == report_id, MemoryReport.user_id == user.uuid)
    )
    if report is None or report.kind != "notice":
        raise _http(404, "not_found", "no such notice")
    report.status = "seen"
    await db.commit()
    return {"id": str(report.id), "status": report.status}


@router.post("/memory/crosscheck", status_code=202)
async def crosscheck_now(request: Request, db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    """Compare the caller's memories now (it needs a node that is available for the model)."""
    settings = await ChatStore(db, user.uuid).get_settings()
    if settings is not None and not settings.memory_enabled:
        raise _http(409, "memory_disabled", "memory is switched off in the settings")
    if not await request.app.state.gateway.any_node_available():
        raise _http(409, "no_node_available", "no node is available to run the model on")
    spawn(request.app.state.memory_care.crosscheck.run(user.uuid, {}))
    return {"started": True}


# --- the tag tree (D74) ---------------------------------------------------------------------------


async def tree_view(tree: TagTree) -> dict[str, Any]:
    """The whole tree, flat (the page nests it by ``parent_id``), with what each tag hands down
    and how many live notes carry it."""
    counts = await tree.counts()
    out = []
    # the fixed top level in its own order, then everything else
    order = {name: n for n, name in enumerate(memory_tags.ROOT_NAMES)}
    tags = sorted(
        await tree.all(), key=lambda t: (t.parent_id is not None, order.get(t.name, 99), t.name)
    )
    for t in tags:
        eff = await tree.effective(t)
        out.append(
            {
                "id": str(t.id),
                "parent_id": str(t.parent_id) if t.parent_id else None,
                "name": t.name,
                "description": t.description,
                "responsible": t.responsible,
                "sensitive": t.sensitive,
                "released": t.released,
                "effective": {
                    "responsible": eff.responsible,
                    "sensitive": eff.sensitive,
                    "released": list(eff.released),
                },
                "created_by": t.created_by,
                "reviewed": t.reviewed,
                "root": t.parent_id is None,
                "notes": counts.get(t.id, 0),
            }
        )
    return {
        "tags": out,
        "programs": list(memory_tags.PROGRAMS),
        "responsible": list(memory_tags.RESPONSIBLE),
        "max_depth": memory_tags.MAX_DEPTH,
    }


def _tag_http(exc: TagError) -> HTTPException:
    status = 404 if exc.code == "not_found" else 409 if exc.code == "exists" else 422
    return _http(status, exc.code, str(exc))


@router.get("/memory/tags")
async def list_tags(db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    tree = TagTree(db, user.uuid)
    await tree.ensure()
    view = await tree_view(tree)
    await db.commit()
    return view


class TagCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=memory_tags.NAME_MAX)
    parent_id: uuid.UUID
    description: str = Field("", max_length=memory_tags.DESCRIPTION_MAX)


@router.post("/memory/tags", status_code=201)
async def create_tag(body: TagCreate, db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    tree = TagTree(db, user.uuid)
    await tree.ensure()
    try:
        await tree.create(body.name, body.parent_id, by="user", description=body.description)
    except TagError as exc:
        raise _tag_http(exc) from None
    view = await tree_view(tree)
    await db.commit()
    return view


class TagUpdate(BaseModel):
    """Only the fields given change; ``responsible``, ``sensitive`` and ``released`` null =
    inherited from the parent."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=memory_tags.NAME_MAX)
    parent_id: uuid.UUID | None = None
    description: str | None = Field(None, max_length=memory_tags.DESCRIPTION_MAX)
    responsible: str | None = Field(None, max_length=40)
    sensitive: bool | None = None
    released: list[str] | None = Field(None, max_length=len(memory_tags.PROGRAMS))
    reviewed: bool | None = None


@router.patch("/memory/tags/{tag_id}")
async def update_tag(
    tag_id: uuid.UUID, body: TagUpdate, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    tree = TagTree(db, user.uuid)
    changes = {k: getattr(body, k) for k in body.model_fields_set}
    try:
        await tree.update(tag_id, **changes)
    except TagError as exc:
        raise _tag_http(exc) from None
    view = await tree_view(tree)
    await db.commit()
    return view


class TagMerge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    into: uuid.UUID


@router.post("/memory/tags/{tag_id}/merge")
async def merge_tag(
    tag_id: uuid.UUID, body: TagMerge, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    """Two tags that mean the same: the notes and children of this one go to ``into``."""
    tree = TagTree(db, user.uuid)
    try:
        await tree.merge(tag_id, body.into)
    except TagError as exc:
        raise _tag_http(exc) from None
    view = await tree_view(tree)
    await db.commit()
    return view


@router.delete("/memory/tags/{tag_id}")
async def delete_tag(tag_id: uuid.UUID, db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    """A tag goes; its notes and children go to its parent (nothing is lost)."""
    tree = TagTree(db, user.uuid)
    try:
        await tree.remove(tag_id)
    except TagError as exc:
        raise _tag_http(exc) from None
    view = await tree_view(tree)
    await db.commit()
    return view


@router.get("/memory/sort")
async def sort_suggestions(db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    """Notes learned before applications existed that look as if they belong somewhere else: a
    suggestion from their words, never an assignment."""
    store = MemoryStore(db, user.uuid)
    out = []
    for i in await store.items(
        kinds=("operational",), limit=2000, application=memory_scope.DEFAULT_APPLICATION
    ):
        suggested = memory_scope.suggest_application(i.text)
        if suggested and suggested != i.application:
            out.append(
                {
                    "id": str(i.id),
                    "text": i.text,
                    "kind": i.kind,
                    "current": i.application,
                    "suggested": suggested,
                }
            )
    return {"suggestions": out}


class SortItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    application: str | None = Field(None, max_length=40)


class SortBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignments: list[SortItem] = Field(max_length=500)


@router.post("/memory/sort")
async def apply_sort(body: SortBody, db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    """The person accepts suggestions (or their own choice) in one go."""
    store = MemoryStore(db, user.uuid)
    moved = 0
    for a in body.assignments:
        key = memory_scope.clean_application(a.application)
        if a.application not in (None, "") and key is None:
            raise _http(422, "application", "an application is a key such as dev, or null")
        try:
            if await store.set_placement(a.id, application=key) is not None:
                moved += 1
        except PlacementError:
            continue
    await db.commit()
    return {"moved": moved}


@router.get("/memory/{item_id}")
async def get_memory_item(
    item_id: uuid.UUID, request: Request, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    store = MemoryStore(db, user.uuid)
    item = await _own(store, item_id)
    names = await store.tag_names([item])
    return {
        **item_view(item, _cfg(request), names.get(item.id)),
        "versions": [version_view(v) for v in await store.versions(item_id)],
    }


@router.patch("/memory/{item_id}")
async def edit_memory_item(
    item_id: uuid.UUID,
    body: ItemUpdate,
    request: Request,
    db: Db,
    user: CurrentUser = Depends(api_user),
) -> Any:
    store = MemoryStore(db, user.uuid)
    item = await _own(store, item_id)
    kwargs: dict[str, Any] = {}
    if body.evidence is not None and body.evidence in EVIDENCE:
        kwargs["evidence"] = body.evidence
    if body.text is not None:
        text = normalise_text(body.text)
        blocked = memory_filter.check(text)
        if blocked:
            await store.add_block(blocked, "user", {"item_id": str(item_id)})
            await db.commit()
            raise _http(422, "secret_blocked", "This text looks like a secret and is not stored.")
        if text != item.text:
            vec, embedder = await _service(request).embed_one(text)
            kwargs.update(text_=text, embedding=vec, embedder=embedder)
    if not kwargs:
        return item_view(item, _cfg(request))
    changed = await store.change(item_id, reason="edited by the user", actor="user", **kwargs)
    await _service(request).heal(store)
    await db.commit()
    assert changed is not None  # noqa: S101 - read for this user just above
    return item_view(changed, _cfg(request))


@router.post("/memory/{item_id}/revert")
async def revert_memory_item(
    item_id: uuid.UUID,
    body: RevertBody,
    request: Request,
    db: Db,
    user: CurrentUser = Depends(api_user),
) -> Any:
    """Go back to the content of an earlier version (a new version is written, the history
    stays). The note becomes active again."""
    store = MemoryStore(db, user.uuid)
    item = await _own(store, item_id)
    old = await store.version(item_id, body.version)
    if old is None:
        raise _http(404, "not_found", "unknown version")
    kwargs: dict[str, Any] = {}
    if old.text != item.text:
        vec, embedder = await _service(request).embed_one(old.text)
        kwargs.update(text_=old.text, embedding=vec, embedder=embedder)
    await store.close_questions_about([item.id], f"the note was reverted to version {body.version}")
    changed = await store.change(
        item_id,
        reason=f"reverted to version {body.version}",
        actor="user",
        evidence=old.evidence,
        confidence=old.confidence,
        status="active",
        disputed_with=None,
        superseded_by=None,
        **kwargs,
    )
    await _service(request).heal(store)
    await db.commit()
    assert changed is not None  # noqa: S101
    return item_view(changed, _cfg(request))


@router.delete("/memory/{item_id}")
async def delete_memory_item(
    item_id: uuid.UUID, request: Request, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    """Soft delete: the note is hidden and can be restored; its history stays."""
    store = MemoryStore(db, user.uuid)
    item = await _own(store, item_id)
    if item.status == "deleted":
        return item_view(item, _cfg(request))
    await store.close_questions_about([item.id], "the note was deleted by the user")
    changed = await store.change(
        item_id,
        reason="deleted by the user",
        actor="user",
        status="deleted",
        disputed_with=None,
        superseded_by=None,
    )
    await _service(request).heal(store)
    await db.commit()
    assert changed is not None  # noqa: S101
    return item_view(changed, _cfg(request))


@router.post("/memory/{item_id}/restore")
async def restore_memory_item(
    item_id: uuid.UUID, request: Request, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    store = MemoryStore(db, user.uuid)
    item = await _own(store, item_id)
    if item.status not in ("deleted", "archived"):
        raise _http(409, "not_deleted", "the note is neither deleted nor archived")
    changed = await store.change(
        item_id,
        reason="restored from the archive by the user"
        if item.status == "archived"
        else "restored by the user",
        actor="user",
        status="active",
    )
    await db.commit()
    assert changed is not None  # noqa: S101
    return item_view(changed, _cfg(request))


@router.post("/memory/{item_id}/resolve")
async def resolve_dispute(
    item_id: uuid.UUID,
    body: ResolveBody,
    request: Request,
    db: Db,
    user: CurrentUser = Depends(api_user),
) -> Any:
    """Decide a dispute the consolidation flagged: keep this note, the other one, or both."""
    store = MemoryStore(db, user.uuid)
    item = await _own(store, item_id)
    partner = await store.get(item.disputed_with) if item.disputed_with else None
    if item.status != "disputed" or partner is None:
        raise _http(409, "not_disputed", "the note is not in dispute")
    why = "dispute resolved by the user"
    if body.keep == "this":
        mine, theirs = "active", "superseded"
    elif body.keep == "other":
        mine, theirs = "superseded", "active"
    else:
        mine, theirs = "active", "active"
    changed = await store.change(
        item_id,
        reason=f"{why}: {body.keep}",
        actor="user",
        status=mine,
        disputed_with=None,
        superseded_by=partner.id if mine == "superseded" else None,
    )
    await store.change(
        partner.id,
        reason=f"{why}: {body.keep}",
        actor="user",
        status=theirs,
        disputed_with=None,
        superseded_by=item.id if theirs == "superseded" else None,
    )
    await store.close_questions_about(
        [item.id, partner.id], "resolved by the user on the memory page"
    )
    await _service(request).heal(store)
    await db.commit()
    assert changed is not None  # noqa: S101
    return item_view(changed, _cfg(request))


@router.post("/memory/{item_id}/undo")
async def undo_correction(
    item_id: uuid.UUID, request: Request, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    """Take back a note the assistant wrote because of a statement (a correction or a new
    fact): it is deleted (restorable) and every note it replaced or withdrew is active again."""
    store = MemoryStore(db, user.uuid)
    item = await _own(store, item_id)
    replaced = await store.superseded_by(item.id)
    await store.close_questions_about([item.id], "the note was taken back by the user")
    changed = item
    if item.status != "deleted":
        changed = await store.change(
            item.id,
            reason="taken back by the user",
            actor="user",
            status="deleted",
            disputed_with=None,
            superseded_by=None,
        )
    for old in replaced:
        await store.change(
            old.id,
            reason=f"the user took back the correction: “{item.text}” is no longer the truth",
            actor="user",
            status="active",
            superseded_by=None,
            disputed_with=None,
        )
    await _service(request).heal(store)
    await db.commit()
    assert changed is not None  # noqa: S101
    return {**item_view(changed, _cfg(request)), "restored": [str(n.id) for n in replaced]}


class FeedbackBody(BaseModel):
    """The user's word on a note put into an answer: it helped, or it did not fit."""

    model_config = ConfigDict(extra="forbid")

    helped: bool


@router.post("/memory/{item_id}/feedback")
async def note_feedback(
    item_id: uuid.UUID,
    body: FeedbackBody,
    request: Request,
    db: Db,
    user: CurrentUser = Depends(api_user),
) -> Any:
    """The user's word that a note helped or did not fit: it counts heavily in the usefulness and
    deletes nothing (a note that is useless, unreliable and idle is archived by the care)."""
    store = MemoryStore(db, user.uuid)
    await _own(store, item_id)
    item = await store.feedback(item_id, body.helped, memory_care.utcnow())
    await db.commit()
    assert item is not None  # noqa: S101
    return item_view(item, _cfg(request))


@router.post("/memory/{item_id}/archive")
async def archive_memory_item(
    item_id: uuid.UUID, request: Request, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    """Put a note away: not recalled any more, still searchable, restorable (a version records
    it). The user can do it by hand; the care does it only for notes nobody needs."""
    store = MemoryStore(db, user.uuid)
    item = await _own(store, item_id)
    if item.status == "archived":
        return item_view(item, _cfg(request))
    if item.status != "active":
        raise _http(409, "not_active", "only an active note can be archived")
    changed = await store.change(
        item_id, reason="archived by the user", actor="user", status="archived"
    )
    await db.commit()
    assert changed is not None  # noqa: S101
    return item_view(changed, _cfg(request))


@router.post("/memory/care", status_code=202)
async def care_now(request: Request, db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    """Start a care run for the caller's notes now (it needs a node that is available)."""
    settings = await ChatStore(db, user.uuid).get_settings()
    if settings is not None and not settings.memory_enabled:
        raise _http(409, "memory_disabled", "memory is switched off in the settings")
    care = request.app.state.memory_care
    if care.is_running(user.uuid) or await MemoryStore(db, user.uuid).running_run("care"):
        raise _http(409, "already_running", "a care run is already in progress")
    if not await request.app.state.gateway.any_node_available():
        raise _http(409, "no_node_available", "no node is available to run the model on")
    spawn(care.run(user.uuid, "manual"))
    return {"started": True}


@router.post("/memory/digest", status_code=202)
async def digest_now(request: Request, db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    """Look at the caller's chats that have no checked summary yet, now (summary by the model of
    the last answer, second look by another model; what the second look learns is stored as
    unverified notes). Each chat still waits for a quiet moment, so this only starts the timers."""
    settings = await ChatStore(db, user.uuid).get_settings()
    if settings is not None and not settings.memory_enabled:
        raise _http(409, "memory_disabled", "memory is switched off in the settings")
    digester = request.app.state.digester
    if not request.app.state.settings.chat_digest_enabled:
        raise _http(409, "digest_disabled", "the chat digest is switched off")
    if not await request.app.state.gateway.any_node_available():
        raise _http(409, "no_node_available", "no node is available to run the model on")
    return {"started": await digester.run_now(user.uuid)}


@router.post("/memory/forget-all")
async def forget_all(db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    """Soft-delete everything (each note keeps its history and can be restored)."""
    count = await MemoryStore(db, user.uuid).forget_all()
    await db.commit()
    return {"forgotten": count}


@router.post("/memory/purge-deleted")
async def purge_deleted(db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    """Permanently remove the notes that were deleted (history included)."""
    count = await MemoryStore(db, user.uuid).purge_deleted()
    await db.commit()
    return {"purged": count}


@router.post("/memory/consolidate", status_code=202)
async def consolidate_now(request: Request, db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    """Start a consolidation run for the caller's notes now. It needs a node that is
    ``available`` (no game, no VRAM pressure) to run the model on."""
    memory = _service(request)
    settings = await ChatStore(db, user.uuid).get_settings()
    if settings is not None and not settings.memory_enabled:
        raise _http(409, "memory_disabled", "memory is switched off in the settings")
    if memory.is_running(user.uuid) or await MemoryStore(db, user.uuid).running_run():
        raise _http(409, "already_running", "a consolidation run is already in progress")
    if not await request.app.state.gateway.any_node_available():
        raise _http(409, "no_node_available", "no node is available to run the model on")
    spawn(memory.consolidate(user.uuid, "manual"))
    return {"started": True}
