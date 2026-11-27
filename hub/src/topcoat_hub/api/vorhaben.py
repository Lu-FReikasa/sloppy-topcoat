"""Large tasks in Projekte (D109, D110, issue #32): the view of a large task's sub-tasks (the API
the view of issue #33 reads), its decomposition, its workshop scope and one step of its run.

The workshop scope is made only here, by the signed-in admin's click on its card: never by an API
key or an agent, and its scope is the hub's (the large task's exact branch, the actions of D110,
14 days, never the push flag), not the request's.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from topcoat_hub import agent_contract, vorhaben
from topcoat_hub.api.mandates import _person
from topcoat_hub.api.project import _refused, _sets, _store
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.models import User
from topcoat_hub.project_mandates import WORKSHOP, mandate_view
from topcoat_hub.project_store import ProjectError

router = APIRouter(prefix="/api/project")


class IssuePipeline:
    """The issue pipeline of the hub as the coordinator drives it (project_runs, issue_fixes)."""

    def __init__(self, runs: Any, fixes: Any) -> None:
        self.runs, self.fixes_service = runs, fixes

    async def start(self, user_id: uuid.UUID, node_id: uuid.UUID) -> None:
        # the program is the one the sub-task's text names (issue_investigate for code, the
        # research report for reading)
        await self.runs.start_node(user_id, node_id)

    async def fixes(self, user_id: uuid.UUID, node_ids: Any) -> dict[str, list[Any]]:
        if self.fixes_service is None:
            return {}
        return {str(n): await self.fixes_service.of_issue(user_id, n) for n in node_ids}


def coordinator(request: Request) -> vorhaben.Coordinator:
    from topcoat_hub.issue_fixes import LIVE_REPO

    state = request.app.state
    return vorhaben.Coordinator(
        _store(request),
        _sets(request),
        IssuePipeline(getattr(state, "project_runs", None), getattr(state, "fixes", None)),
        repo=LIVE_REPO,
        contract=agent_contract.long("dev"),
        bus=getattr(state, "bus", None),
    )


def _ports(request: Request, user_id: uuid.UUID, large: Any) -> tuple[Any, Any, Any]:
    """``(ask, read, outline)`` of the hub: a block program on the person's models, the live code
    and its map in the workshop."""
    runs, fixes = request.app.state.project_runs, request.app.state.fixes

    async def read(paths: list[str]) -> dict[str, str | None]:
        return await fixes.read_live(user_id, paths)

    async def outline(path: str) -> list[dict[str, Any]]:
        return await fixes.outline_live(user_id, path)

    async def ask(name: str, inputs: dict[str, Any]) -> tuple[str, str]:
        return await (await runs._asker(user_id, large))(name, inputs)

    return ask, read, outline


def _decompose_refused(exc: vorhaben.DecomposeError) -> HTTPException:
    status = 422 if exc.code == "refused" else 409
    return HTTPException(
        status, {"code": exc.code, "message": exc.message, "problems": exc.problems}
    )


@router.get("/nodes/{node_id}/large-task")
async def read_large_task(
    node_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The large task with its sub-tasks, their states, results and next steps (issue #33)."""
    try:
        return await coordinator(request).view(user.uuid, node_id)
    except ProjectError as exc:
        raise _refused(exc) from exc


@router.post("/nodes/{node_id}/decompose", status_code=201)
async def decompose_large_task(
    node_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Cut the task into sub-tasks (one run of ``decompose``, checked by the hub). Filed at once
    under the grant ``project: {decompose}`` at level free, otherwise a change set to confirm."""
    coord = coordinator(request)
    try:
        large = await coord.store.get_node(user.uuid, node_id)
        ask, read, outline = _ports(request, user.uuid, large)
        out = await coord.decompose(user.uuid, node_id, ask, read, outline)
    except vorhaben.DecomposeError as exc:
        raise _decompose_refused(exc) from exc
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {**out, "large_task": await coord.view(user.uuid, node_id)}


@router.post("/nodes/{node_id}/workshop-scope", status_code=201)
async def give_workshop_scope(
    node_id: uuid.UUID,
    request: Request,
    language: str | None = None,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The admin's click on the card of a large task's workshop scope (D110). The request names
    nothing but the large task and the language the card was shown in (``en``/``de``; without it
    the person's stored one, else English), so the stored text is the one the card showed (#190);
    branch, actions, duration and the missing push flag are the hub's."""
    from topcoat_hub.issue_fixes import LIVE_REPO

    _person(user)
    store = _store(request)
    if language not in vorhaben.LANGUAGES:
        async with request.app.state.sessionmaker() as db:
            row = await db.get(User, user.uuid)
        language = (row.language if row else None) or "en"
    try:
        large = await store.get_node(user.uuid, node_id)
        if large.kind != "task":
            raise HTTPException(409, {"code": "not_task", "message": "only a task has a scope"})
        mandate = await store.create_mandate(
            user.uuid,
            large.project_id,
            repo=LIVE_REPO,
            branch=vorhaben.branch_of(large.number),
            actions=[WORKSHOP],
            push=False,
            text=vorhaben.scope_text(large.number, large.title, LIVE_REPO, language),
            days=vorhaben.DAYS,
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"mandate": mandate_view(mandate)}


@router.post("/nodes/{node_id}/large-task/tick")
async def tick_large_task(
    node_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """One step of the run: replan a stopped branch, start what is ready. Starts nothing that
    writes without the workshop scope; the scope is checked on every step."""
    coord = coordinator(request)
    try:
        large = await coord.store.get_node(user.uuid, node_id)
        ask, read, _ = _ports(request, user.uuid, large)
        done = await coord.tick(user.uuid, node_id, ask, read)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {
        "tick": {
            "started": done.started,
            "replanned": done.replanned,
            "blocked": done.blocked,
            "stopped": done.stopped,
            "done": done.done,
        },
        "large_task": await coord.view(user.uuid, node_id),
    }
