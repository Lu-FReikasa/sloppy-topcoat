"""The agent's tasks over REST (E3-01a): start one, watch it, cancel it, continue it.

Every route is scoped to the caller (D20): a task of another user is 404, whatever its id. An API
key may start and read tasks too, but never with the rights of an admin (see ``Caller.of``).
"""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import research_profile
from topcoat_hub.agent_prompts import TOOL_MODES
from topcoat_hub.agent_service import TOOL_MODES_KEY, AgentError, AgentService
from topcoat_hub.agent_store import AgentStore
from topcoat_hub.auth import CurrentUser, api_user, require_admin
from topcoat_hub.chat_store import get_hub_setting, set_hub_setting
from topcoat_hub.db import get_db
from topcoat_hub.models import AgentEvidence, AgentStep, AgentTask
from topcoat_hub.proposals import ProposalError, ProposalService
from topcoat_hub.tools import Caller

router = APIRouter(prefix="/api/agent")


def _service(request: Request) -> AgentService:
    return request.app.state.agents


def _proposals(request: Request) -> ProposalService:
    return request.app.state.proposals


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


class ManualProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: uuid.UUID
    capability: str = Field(min_length=3, max_length=100)
    input: dict[str, Any] = Field(default_factory=dict)


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=4000)
    model: str | None = Field(None, max_length=255)
    # E3-08: split the question across the machines and merge the reports
    coordinator: bool = False
    # a task of the program Research: its tools are the program's ceiling, `depth` sets the frame
    # (a thorough one ends as a report in the Ablage, D102)
    application: Literal["research"] | None = None
    depth: Literal["quick", "default", "thorough"] | None = None


def task_view(t: AgentTask) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "title": t.title,
        "prompt": t.prompt,
        "model": t.model,
        "via": t.via,
        "origin": t.origin,
        "kind": t.kind,
        "parent_id": str(t.parent_id) if t.parent_id else None,
        "conversation_id": str(t.conversation_id) if t.conversation_id else None,
        "deadline_at": _iso(t.deadline_at),
        "status": t.status,
        "stop_reason": t.stop_reason,
        "error": t.error,
        "summary": t.summary,
        "diagnosis": t.diagnosis,
        "step_count": t.step_count,
        "tool_calls": t.tool_calls,
        "scope": t.scope,
        "runs": t.runs,
        "created_at": _iso(t.created_at),
        "started_at": _iso(t.started_at),
        "finished_at": _iso(t.finished_at),
        "updated_at": _iso(t.updated_at),
    }


def child_view(c: AgentTask) -> dict[str, Any]:
    """What the coordinator's page shows of one check (the check's own page has the rest)."""
    nodes = (c.scope or {}).get("nodes") or []
    return {
        "id": str(c.id),
        "node": nodes[0] if nodes else None,
        "question": c.prompt,
        "status": c.status,
        "stop_reason": c.stop_reason,
        "summary": c.summary,
        "step_count": c.step_count,
        "tool_calls": c.tool_calls,
    }


def step_view(s: AgentStep) -> dict[str, Any]:
    return {"seq": s.seq, "kind": s.kind, "data": s.data, "created_at": _iso(s.created_at)}


def evidence_view(e: AgentEvidence) -> dict[str, Any]:
    source = {k: v for k, v in e.source.items() if k not in ("key", "node_id")}
    return {
        "ref": e.ref,
        "tag": e.tag,
        "source": source,
        "excerpt": e.excerpt,
        "bytes": e.bytes,
        "created_at": _iso(e.created_at),
    }


def _refused(exc: AgentError) -> HTTPException:
    return _error(exc.status, exc.code, exc.message)


@router.post("/tasks", status_code=201)
async def start_task(
    body: StartRequest,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    prompt = body.prompt.strip()
    if body.depth is not None and body.application != "research":
        raise _error(422, "invalid_input", "depth belongs to application research")
    if body.application == "research":
        if body.coordinator:
            raise _error(422, "invalid_input", "a research task is not a coordinator")
        if body.depth is not None:
            prompt = f"{research_profile.depth_line(body.depth)}\n{prompt}"
    try:
        task = await _service(request).start(
            user,
            prompt,
            body.model,
            kind="coordinator" if body.coordinator else "single",
            application=body.application,
        )
    except AgentError as exc:
        raise _refused(exc) from exc
    return task_view(task)


@router.get("/tasks")
async def list_tasks(
    limit: int = Query(100, ge=1, le=500),
    include_chat: bool = Query(False, description="also the checks that chat answers started"),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(api_user),
) -> list[dict[str, Any]]:
    origin = None if include_chat else "tasks"
    return [task_view(t) for t in await AgentStore(db, user.uuid).list(limit, origin)]


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    store = AgentStore(db, user.uuid)
    task = await store.get(task_id)
    if task is None:
        raise _error(404, "not_found", "unknown task")
    return {
        **task_view(task),
        "steps": [step_view(s) for s in await store.steps(task_id)],
        "evidence": [evidence_view(e) for e in await store.evidence(task_id)],
        "proposals": await _proposals(request).for_task(user.uuid, task_id),
        "children": [child_view(c) for c in await store.children(task_id)],
    }


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    task = await _service(request).cancel(user.uuid, task_id)
    if task is None:
        raise _error(404, "not_found", "unknown task")
    return task_view(task)


@router.post("/tasks/{task_id}/continue")
async def continue_task(
    task_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        task = await _service(request).resume(user, task_id)
    except AgentError as exc:
        raise _refused(exc) from exc
    if task is None:
        raise _error(404, "not_found", "unknown task")
    return task_view(task)


# --- proposals (E3-01b): the user decides, one click per proposal --------------------------------


def _signed_in(user: CurrentUser) -> None:
    """Only the signed-in user confirms or refuses a proposal. An API key — an admin's included —
    never does (D15): the click is a person's."""
    if user.via_key:
        raise _error(403, "gui_only", "only the signed-in user confirms or refuses a proposal")


@router.get("/proposals/pending")
async def pending_proposals(
    request: Request, user: CurrentUser = Depends(api_user)
) -> list[dict[str, Any]]:
    """The proposals of the signed-in user that wait for a click (the start page lists them)."""
    return await _proposals(request).pending(user.uuid)


@router.get("/tasks/{task_id}/proposals")
async def task_proposals(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(api_user),
) -> list[dict[str, Any]]:
    if await AgentStore(db, user.uuid).get(task_id) is None:
        raise _error(404, "not_found", "unknown task")
    return await _proposals(request).for_task(user.uuid, task_id)


@router.post("/proposals/manual")
async def manual_proposal(
    body: ManualProposalRequest, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The person asks for one change themselves (the inventory's restart buttons). The answer is
    a pending proposal like the agent's: nothing has run; the click on ``execute`` does."""
    _signed_in(user)
    try:
        return await _proposals(request).create_manual(
            Caller.of(user), body.node_id, body.capability, body.input
        )
    except ProposalError as exc:
        raise _error(exc.status, exc.code, exc.message) from exc


@router.get("/proposals/{proposal_id}")
async def get_proposal(
    proposal_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        return await _proposals(request).get(user.uuid, proposal_id)
    except ProposalError as exc:
        raise _error(exc.status, exc.code, exc.message) from exc


@router.post("/proposals/{proposal_id}/execute")
async def execute_proposal(
    proposal_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    _signed_in(user)
    try:
        return await _proposals(request).execute(Caller.of(user), proposal_id)
    except ProposalError as exc:
        raise _error(exc.status, exc.code, exc.message) from exc


@router.post("/proposals/{proposal_id}/refuse")
async def refuse_proposal(
    proposal_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    _signed_in(user)
    try:
        return await _proposals(request).refuse(user.uuid, proposal_id)
    except ProposalError as exc:
        raise _error(exc.status, exc.code, exc.message) from exc


# --- how the agent talks to a model ---------------------------------------------------------


class ToolModes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_modes: dict[str, str] = Field(default_factory=dict, max_length=200)


async def _tool_modes(request: Request, db: AsyncSession) -> dict[str, Any]:
    value = await get_hub_setting(db, TOOL_MODES_KEY)
    modes = {k: v for k, v in value.items() if v in TOOL_MODES} if isinstance(value, dict) else {}
    return {
        "tool_modes": modes,
        "default_tool_mode": request.app.state.settings.agent_tool_mode,
        "modes": list(TOOL_MODES),
    }


@router.get("/settings")
async def get_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The tool mode per model: ``native`` (OpenAI tool calls, llama-server --jinja), ``json`` (the
    prompt-based protocol) or ``auto`` (native, JSON for a model that cannot)."""
    return await _tool_modes(request, db)


@router.put("/settings")
async def put_settings(
    body: ToolModes,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Set the tool mode per model (admin, dev token); a model not listed uses the default."""
    for model, mode in body.tool_modes.items():
        if not model.strip() or len(model) > 255 or mode not in TOOL_MODES:
            raise _error(
                422, "invalid_tool_mode", f"{model!r}: the mode must be one of {list(TOOL_MODES)}"
            )
    await set_hub_setting(db, TOOL_MODES_KEY, body.tool_modes or None)
    await db.commit()
    return await _tool_modes(request, db)
