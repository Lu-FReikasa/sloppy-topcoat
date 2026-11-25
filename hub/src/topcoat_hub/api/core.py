"""The core and the page "Anweisungen" (E3-06, ARCHITECTURE D38).

What steers the assistant — style, rules, a profile, the household's facts, the fixed system
rules, the last conversation's state — and the queue of changes the assistant proposed. The user
sees and changes **their own** (an admin too: nobody reads another user's core). The household is
readable by everybody and written by an admin only; the system rules are readable by everybody and
changed by an admin only — the assistant has no way to write either.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import core_rules, memory_scope
from topcoat_hub.api.memory import item_view
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.chat_store import ChatStore
from topcoat_hub.digest_store import DigestStore
from topcoat_hub.memory_store import (
    CORE_KINDS,
    FACT_KINDS,
    MemoryStore,
    ProtectedWrite,
    SecretBlocked,
)
from topcoat_hub.models import HOUSEHOLD_ID, SYSTEM_ID, MemoryItem, MemoryProposal

router = APIRouter(prefix="/api/core")


def _http(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


async def _db(request: Request) -> Any:
    async with request.app.state.sessionmaker() as db:
        yield db


Db = Annotated[AsyncSession, Depends(_db)]


def _is_admin(user: CurrentUser) -> bool:
    """An admin, and not an API key (a key never inherits admin rights)."""
    return user.is_admin and not user.via_key


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt else None


def proposal_view(p: MemoryProposal) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "action": p.action,
        "kind": p.kind,
        "text": p.text,
        "example": p.example,
        "core": p.core,
        "weak": p.weak,
        "reason": p.reason,
        "evidence": p.evidence,
        "diff": p.diff,
        "target_id": str(p.target_id) if p.target_id else None,
        "origin": p.origin,
        "status": p.status,
        "created_at": _iso(p.created_at),
        "decided_at": _iso(p.decided_at),
    }


class ItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["style", "rule"]
    text: str = Field(min_length=1, max_length=1000)
    example: str | None = Field(None, max_length=core_rules.EXAMPLE_MAX_CHARS * 2)
    core: bool = True


class ItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(None, min_length=1, max_length=1000)
    example: str | None = Field(None, max_length=core_rules.EXAMPLE_MAX_CHARS * 2)
    core: bool | None = None


class HouseholdCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1000)
    core: bool = True


class SystemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=3000)


class AcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(None, min_length=1, max_length=1000)
    example: str | None = Field(None, max_length=core_rules.EXAMPLE_MAX_CHARS * 2)


def _refused(exc: Exception) -> HTTPException:
    if isinstance(exc, SecretBlocked):
        return _http(
            422, "secret_blocked", f"This text looks like a secret ({exc.rule}) and is not stored."
        )
    return _http(403, "protected", str(exc))


async def _own(store: MemoryStore, item_id: uuid.UUID) -> MemoryItem:
    item = await store.get(item_id)
    if item is None or item.scope != "user":
        raise _http(404, "not_found", "unknown note")
    return item


@router.get("")
async def overview(
    request: Request,
    db: Db,
    user: CurrentUser = Depends(api_user),
    context: Annotated[int, Query(ge=1024, le=1_000_000)] = 16384,
) -> Any:
    """The core of the caller: its parts with their budget, the proposals waiting for a click,
    and ``preview`` — the block exactly as the assistant gets it in a new conversation."""
    core = request.app.state.core
    store = MemoryStore(db, user.uuid)
    settings = await ChatStore(db, user.uuid).get_settings()
    memory_enabled = settings.memory_enabled if settings else True
    await core.ensure_user(db, user.uuid)
    await db.commit()
    block = await core.preview(user.uuid, context, memory_enabled)
    cfg = core.care_config
    included = {i for ids in block.included.values() for i in ids}

    def view(i: MemoryItem) -> dict[str, Any]:
        return {**item_view(i, cfg), "in_core": str(i.id) in included}

    rules = await store.instructions()
    pinned = await store.pinned_facts()
    profile = await store.profile() if memory_enabled else []
    seen = {i.id for i in pinned}
    facts = [*pinned, *[i for i in profile if i.id not in seen]]
    system = await store.system_rules()
    return {
        "budget": {
            "total": block.budget,
            "used": block.total,
            "context": context,
            "parts": {
                name: {"budget": block.shares.get(name, 0), "used": block.used.get(name, 0)}
                for name in ("system", "persona", "rules", "profile", "household", "recent")
            },
            "cut": block.cut,
            "examples_dropped": block.examples_dropped,
        },
        "rules": [view(i) for i in rules],
        "profile": [view(i) for i in core_rules.ranked(facts, datetime.now(UTC), cfg)],
        "household": [view(i) for i in await store.household()],
        "system": [
            {
                "id": str(i.id),
                "lang": (i.source_ref or {}).get("lang"),
                "text": i.text,
                "version": i.version,
            }
            for i in system
        ],
        "recent": [
            {"id": c.id, "title": c.title, "day": c.day, "statements": list(c.statements)}
            for c in await DigestStore(db, user.uuid).recent(
                memory_scope.DEFAULT_APPLICATION,
                None,
                5,
                core.settings.core_recent_days,
            )
        ],
        "proposals": [proposal_view(p) for p in await store.proposals("proposed")],
        "preview": block.text,
        "admin": _is_admin(user),
    }


@router.post("/items", status_code=201)
async def create_item(
    body: ItemCreate, request: Request, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    """The user writes an instruction themself: that is the confirmation, no proposal needed."""
    store = MemoryStore(db, user.uuid)
    try:
        item = await store.add(
            body.text.strip(),
            embedding=None,
            embedder=None,
            evidence="doc",
            confidence=1.0,
            source_type="user",
            source_ref={},
            reason="written by the user on the instructions page",
            actor="user",
            kind=body.kind,
            core=body.core,
            example=body.example,
        )
    except (ProtectedWrite, SecretBlocked) as exc:
        await store.add_block(getattr(exc, "rule", "protected"), "instructions", {})
        await db.commit()
        raise _refused(exc) from exc
    await db.commit()
    await _write_through(request, user, item)
    return item_view(item, request.app.state.core.care_config)


@router.patch("/items/{item_id}")
async def update_item(
    item_id: uuid.UUID,
    body: ItemUpdate,
    request: Request,
    db: Db,
    user: CurrentUser = Depends(api_user),
) -> Any:
    """Change an instruction (text, example) or pin / unpin a note: the user's own click."""
    store = MemoryStore(db, user.uuid)
    item = await _own(store, item_id)
    if item.kind not in (*CORE_KINDS, *FACT_KINDS):
        raise _http(409, "not_editable", "this kind of note is written by the assistant")
    kwargs: dict[str, Any] = {}
    if body.text is not None:
        kwargs["text_"] = body.text.strip()
    if "example" in body.model_fields_set:
        kwargs["example"] = body.example
    if body.core is not None:
        kwargs["core"] = body.core
    if not kwargs:
        return item_view(item, request.app.state.core.care_config)
    try:
        changed = await store.change(
            item_id,
            reason="changed by the user on the instructions page",
            actor="user",
            **kwargs,
        )
    except (ProtectedWrite, SecretBlocked) as exc:
        raise _refused(exc) from exc
    await db.commit()
    assert changed is not None  # noqa: S101
    await _write_through(request, user, changed)
    return item_view(changed, request.app.state.core.care_config)


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: uuid.UUID, request: Request, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    store = MemoryStore(db, user.uuid)
    await _own(store, item_id)
    item = await store.change(
        item_id,
        reason="removed by the user on the instructions page",
        actor="user",
        status="deleted",
    )
    await db.commit()
    assert item is not None  # noqa: S101
    return item_view(item, request.app.state.core.care_config)


async def _write_through(request: Request, user: CurrentUser, item: MemoryItem) -> None:
    """An edit of the note that mirrors the old chat setting (system prompt / form of address)
    is written back to the setting, so the old API shows what the page shows."""
    key = (item.source_ref or {}).get("legacy")
    if key not in ("system_prompt", "address"):
        return
    async with request.app.state.sessionmaker() as db:
        chat_store = ChatStore(db, user.uuid)
        row = await chat_store.get_settings()
        if row is None:
            return
        if key == "system_prompt":
            row.system_prompt = item.text if item.status == "active" else None
        else:
            low = item.text.lower()
            row.address = (
                "sie" if ("sie" in low and "formal" in low and "informal" not in low) else "du"
            )
        await db.commit()


# --- household and system rules: an admin writes, everybody reads -----------------------


def _admin_store(db: AsyncSession, user: CurrentUser, owner: uuid.UUID) -> MemoryStore:
    if not _is_admin(user):
        raise _http(403, "forbidden", "only an admin changes the household and the system rules")
    return MemoryStore(db, owner, acting_admin=True)


@router.post("/household/items", status_code=201)
async def create_household_item(
    body: HouseholdCreate, request: Request, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    store = _admin_store(db, user, HOUSEHOLD_ID)
    try:
        item = await store.add(
            body.text.strip(),
            embedding=None,
            embedder=None,
            evidence="doc",
            confidence=1.0,
            source_type="user",
            source_ref={"by": str(user.uuid)},
            reason="written by an admin",
            actor="admin",
            kind="operational",
            core=body.core,
        )
    except (ProtectedWrite, SecretBlocked) as exc:
        raise _refused(exc) from exc
    await db.commit()
    return item_view(item, request.app.state.core.care_config)


@router.patch("/household/items/{item_id}")
async def update_household_item(
    item_id: uuid.UUID,
    body: ItemUpdate,
    request: Request,
    db: Db,
    user: CurrentUser = Depends(api_user),
) -> Any:
    store = _admin_store(db, user, HOUSEHOLD_ID)
    if await store.get(item_id) is None:
        raise _http(404, "not_found", "unknown household note")
    try:
        item = await store.change(
            item_id,
            reason="changed by an admin",
            actor="admin",
            text_=body.text.strip() if body.text else None,
            core=body.core,
        )
    except (ProtectedWrite, SecretBlocked) as exc:
        raise _refused(exc) from exc
    await db.commit()
    assert item is not None  # noqa: S101
    return item_view(item, request.app.state.core.care_config)


@router.delete("/household/items/{item_id}")
async def delete_household_item(
    item_id: uuid.UUID, request: Request, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    store = _admin_store(db, user, HOUSEHOLD_ID)
    if await store.get(item_id) is None:
        raise _http(404, "not_found", "unknown household note")
    item = await store.change(
        item_id, reason="removed by an admin", actor="admin", status="deleted"
    )
    await db.commit()
    assert item is not None  # noqa: S101
    return item_view(item, request.app.state.core.care_config)


@router.put("/system/{lang}")
async def put_system_rules(
    lang: Literal["en", "de"],
    body: SystemUpdate,
    request: Request,
    db: Db,
    user: CurrentUser = Depends(api_user),
) -> Any:
    """A new version of the fixed rule block of one language (an admin; never the assistant)."""
    store = _admin_store(db, user, SYSTEM_ID)
    rows = await store.system_rules(lang)
    if not rows:
        raise _http(404, "not_found", "no system rules of that language")
    try:
        item = await store.change(
            rows[0].id, reason="changed by an admin", actor="admin", text_=body.text.strip()
        )
    except (ProtectedWrite, SecretBlocked) as exc:
        raise _refused(exc) from exc
    await db.commit()
    assert item is not None  # noqa: S101
    return {"id": str(item.id), "lang": lang, "text": item.text, "version": item.version}


@router.get("/system/{lang}/versions")
async def system_versions(
    lang: Literal["en", "de"], db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    store = MemoryStore(db, user.uuid)
    rows = await store.system_rules(lang)
    if not rows:
        return []
    versions = await MemoryStore(db, SYSTEM_ID).versions(rows[0].id)
    return [
        {
            "version": v.version,
            "text": v.text,
            "actor": v.actor,
            "reason": v.reason,
            "created_at": _iso(v.created_at),
        }
        for v in versions
    ]


# --- the proposals waiting for a click --------------------------------------------------


@router.get("/proposals")
async def list_proposals(db: Db, user: CurrentUser = Depends(api_user)) -> Any:
    return [proposal_view(p) for p in await MemoryStore(db, user.uuid).proposals("proposed")]


@router.post("/proposals/{proposal_id}/accept")
async def accept_proposal(
    proposal_id: uuid.UUID,
    request: Request,
    db: Db,
    body: AcceptBody | None = None,
    user: CurrentUser = Depends(api_user),
) -> Any:
    """One click: the proposal takes effect as a version (undo it with ``revert`` / ``restore``);
    with ``text`` / ``example`` the user edited it first."""
    store = MemoryStore(db, user.uuid)
    body = body or AcceptBody()
    try:
        done = await store.accept_proposal(
            proposal_id,
            text_=body.text,
            **({"example": body.example} if body and "example" in body.model_fields_set else {}),
        )
    except (ProtectedWrite, SecretBlocked) as exc:
        raise _refused(exc) from exc
    if done is None:
        raise _http(404, "not_found", "no such open proposal")
    proposal, item = done
    await db.commit()
    return {
        "proposal": proposal_view(proposal),
        "item": item_view(item, request.app.state.core.care_config) if item else None,
    }


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: uuid.UUID, db: Db, user: CurrentUser = Depends(api_user)
) -> Any:
    """No: the same proposal does not come back for 90 days."""
    proposal = await MemoryStore(db, user.uuid).reject_proposal(proposal_id)
    if proposal is None:
        raise _http(404, "not_found", "no such open proposal")
    await db.commit()
    return proposal_view(proposal)
