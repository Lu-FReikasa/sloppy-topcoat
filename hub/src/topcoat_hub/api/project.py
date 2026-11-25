"""The Projekte application's REST API (brief PPH-01-planning-app, 01b; rework 2026-10-27).

Every route is scoped to the caller (D20) through :class:`ProjectStore`: somebody else's project
or node is simply not found. Every response carries ``revision`` so the web side can detect a
conflict; a write with a stale revision answers 409 and changes nothing. The scope of a project
is set only here, by the person (``PUT /projects/{id}/scope``), never by the project agent.
"""

import contextlib
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from topcoat_hub.agent_service import AgentError
from topcoat_hub.api.agent import task_view as agent_task_view
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.message_service import MessageService
from topcoat_hub.project_base import user_zone
from topcoat_hub.project_changes import APPLIED, FAILED, ChangeError, ChangeStore
from topcoat_hub.project_changes import view as change_view
from topcoat_hub.project_changesets import ChangeSetError, ChangeSetStore
from topcoat_hub.project_changesets import view as changeset_view
from topcoat_hub.project_digest import project_digest
from topcoat_hub.project_events import KINDS, PERSON
from topcoat_hub.project_events import view as event_view
from topcoat_hub.project_filter import FilterError, help_text
from topcoat_hub.project_iterations import IterationStore, iteration_view, results_of
from topcoat_hub.project_progress import progress
from topcoat_hub.project_runs import ProgramsBusy, RunError, batch_view
from topcoat_hub.project_sort import SortError
from topcoat_hub.project_store import (
    APPLICATION,
    Conflict,
    NotFound,
    ProjectError,
    ProjectStore,
)
from topcoat_hub.project_tick import read_prefs, write_prefs
from topcoat_hub.project_versions import version_view
from topcoat_hub.project_views import (
    label_view,
    node_view,
    project_view,
    relation_view,
    reminder_view,
)
from topcoat_hub.tools import Caller, ToolError

router = APIRouter(prefix="/api/project")

Text = Field(default="", max_length=4000)


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Text
    parent_id: uuid.UUID | None = None
    favorite: bool = False
    color: str | None = Field(default=None, max_length=20)
    identifier: str | None = Field(default=None, max_length=10)


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    status: Literal["active", "paused", "done", "dropped"] | None = None
    favorite: bool | None = None
    color: str | None = Field(default=None, max_length=20)
    identifier: str | None = Field(default=None, max_length=10)
    parent_id: uuid.UUID | None = None


class ScopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int
    scope: dict[str, Any]


class NodeCreate(BaseModel):
    """A topic, a task or a report. A report needs ``report_kind``; a topic or task does not."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["topic", "task", "report"]
    title: str = Field(min_length=1, max_length=200)
    body: str = Text
    parent_id: uuid.UUID | None = None
    depends_on: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    report_kind: Literal["note", "question", "decision", "document", "fact"] | None = None
    start_at: datetime | None = None
    due_at: datetime | None = None
    end_at: datetime | None = None
    priority: int | None = Field(default=None, ge=0, le=5)
    percent_done: int | None = Field(default=None, ge=0, le=100)
    color: str | None = Field(default=None, max_length=20)
    estimate_minutes: int | None = Field(default=None, ge=0)
    repeat_rule: dict[str, Any] | None = None
    is_milestone: bool | None = None
    favorite: bool | None = None
    as_of: datetime | None = None
    origin_app: str | None = Field(default=None, max_length=40)


class NodePatch(BaseModel):
    """Only the fields the body sends are changed."""

    model_config = ConfigDict(extra="forbid")

    revision: int
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=4000)
    status: Literal["todo", "in_progress", "in_review", "blocked", "done", "dropped"] | None = None
    depends_on: list[uuid.UUID] | None = Field(default=None, max_length=100)
    start_at: datetime | None = None
    due_at: datetime | None = None
    end_at: datetime | None = None
    priority: int | None = Field(default=None, ge=0, le=5)
    percent_done: int | None = Field(default=None, ge=0, le=100)
    color: str | None = Field(default=None, max_length=20)
    estimate_minutes: int | None = Field(default=None, ge=0)
    repeat_rule: dict[str, Any] | None = None
    is_milestone: bool | None = None
    favorite: bool | None = None
    as_of: datetime | None = None
    origin_app: str | None = Field(default=None, max_length=40)


FIELD_NAMES = (
    "start_at", "due_at", "end_at", "priority", "percent_done", "color", "estimate_minutes",
    "repeat_rule", "is_milestone", "favorite", "as_of", "origin_app",
)  # fmt: skip


def _fields_of(body: BaseModel) -> dict[str, Any]:
    """The node fields a body sent. A field that was sent as null is cleared where the field may be
    empty (a date, a colour, a repeat rule), so null means something for those."""
    sent = body.model_fields_set
    return {name: getattr(body, name) for name in FIELD_NAMES if name in sent}


class MoveBody(BaseModel):
    """``parent_id: null`` moves a node to the top of its project."""

    model_config = ConfigDict(extra="forbid")

    revision: int
    parent_id: uuid.UUID | None


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=4000)


def _store(request: Request) -> ProjectStore:
    return ProjectStore(
        request.app.state.sessionmaker, PERSON, request.app.state.settings.hub_timezone
    )


def _refused(exc: ProjectError) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(404, detail={"code": "not_found", "message": "there is no such item"})
    if isinstance(exc, Conflict):
        return HTTPException(
            409,
            detail={
                "code": "conflict",
                "message": "the item changed since it was read; reload and decide again",
            },
        )
    return HTTPException(422, detail={"code": "invalid", "message": str(exc)})


@router.get("/projects")
async def list_projects(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    projects = await _store(request).list_projects(user.uuid)
    return {"projects": [project_view(p) for p in projects]}


@router.post("/projects", status_code=201)
async def create_project(
    body: ProjectCreate, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        project = await _store(request).create_project(
            user.uuid,
            title=body.title,
            description=body.description,
            parent_id=body.parent_id,
            favorite=body.favorite,
            color=body.color,
            identifier=body.identifier,
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    return project_view(project)


@router.get("/projects/{project_id}")
async def read_project(
    project_id: uuid.UUID,
    request: Request,
    trashed: bool = False,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    store = _store(request)
    try:
        project = await store.get_project(user.uuid, project_id)
        nodes = await store.tree(user.uuid, project_id, include_trashed=trashed)
        labels = await store.labels_for_nodes(user.uuid, [n.id for n in nodes])
    except ProjectError as exc:
        raise _refused(exc) from exc
    done = progress(nodes)
    return {
        "project": project_view(project),
        "nodes": [
            node_view(n, project.title, progress=done.get(n.id), labels=labels.get(n.id, []))
            for n in nodes
        ],
    }


@router.get("/projects/{project_id}/timeline")
async def read_timeline(
    project_id: uuid.UUID,
    request: Request,
    after: int = Query(0, ge=0),
    kinds: str = Query("", max_length=600),
    node: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The Akte of a project: what happened to it, oldest first, in pages (`after` is a `seq`)."""
    wanted = [k for k in kinds.split(",") if k]
    if any(k not in KINDS for k in wanted):
        raise HTTPException(422, detail={"code": "invalid", "message": "unknown event kind"})
    try:
        events = await _store(request).timeline(
            user.uuid, project_id, after=after, kinds=wanted or None, node_id=node, limit=limit
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {
        "events": [event_view(e) for e in events],
        "next_after": events[-1].seq if len(events) == limit else None,
    }


@router.get("/projects/{project_id}/timeline/verify")
async def verify_timeline(
    project_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Check the hash chain of the project's events: a changed or missing line shows up here."""
    try:
        problems = await _store(request).verify_chain(user.uuid, project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"ok": not problems, "problems": problems[:20]}


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: uuid.UUID,
    body: ProjectPatch,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for name in body.model_fields_set - {"revision"}:
        value = getattr(body, name)
        if value is not None or name in ("color", "identifier", "parent_id"):
            changes[name] = value  # null clears a colour, an identifier or a parent project
    try:
        project = await _store(request).update_project(
            user.uuid, project_id, revision=body.revision, **changes
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    return project_view(project)


@router.put("/projects/{project_id}/scope")
async def set_scope(
    project_id: uuid.UUID,
    body: ScopeBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    try:
        project = await _store(request).set_scope(
            user.uuid, project_id, revision=body.revision, scope=body.scope
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    return project_view(project)


@router.post("/projects/{project_id}/nodes", status_code=201)
async def create_node(
    project_id: uuid.UUID,
    body: NodeCreate,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    store = _store(request)
    try:
        node = await store.create_node(
            user.uuid,
            project_id,
            kind=body.kind,
            title=body.title,
            body=body.body,
            parent_id=body.parent_id,
            depends_on=body.depends_on,
            report_kind=body.report_kind,
            fields=_fields_of(body),
        )
        project = await store.get_project(user.uuid, project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return node_view(node, project.title)


@router.patch("/nodes/{node_id}")
async def update_node(
    node_id: uuid.UUID,
    body: NodePatch,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    sent = body.model_fields_set - {"revision"}
    kwargs: dict[str, Any] = {}
    for name in ("title", "body", "status"):
        if name in sent and getattr(body, name) is not None:
            kwargs[name] = getattr(body, name)
    if "depends_on" in sent and body.depends_on is not None:
        kwargs["depends_on"] = body.depends_on
    store = _store(request)
    try:
        node = await store.update_node(
            user.uuid, node_id, revision=body.revision, fields=_fields_of(body), **kwargs
        )
        project = await store.get_project(user.uuid, node.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return node_view(node, project.title)


class CompleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int


@router.post("/nodes/{node_id}/complete")
async def complete_node(
    node_id: uuid.UUID,
    body: CompleteBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """Mark a task done. A repeating task comes back as ``todo`` with its dates moved (04h)."""
    store = _store(request)
    try:
        node = await store.complete_node(user.uuid, node_id, revision=body.revision)
        project = await store.get_project(user.uuid, node.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {**node_view(node, project.title), "reopened": node.status != "done"}


class BatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)  # run in this order
    max_runs: int = Field(ge=1, le=50)  # the limit the person approves


def _run_refused(exc: RunError) -> HTTPException:
    return HTTPException(409, detail={"code": "not_runnable", "message": str(exc)})


BUSY_RETRY_SECONDS = 60


def _busy() -> HTTPException:
    """Every place of the person's programs is taken (issue 2.16, live 2026-11-21: an HTTP 500 with
    a traceback in the log): a clear refusal the page and a script can wait on."""
    return HTTPException(
        429,
        detail={"code": "busy", "message": "too many runs are going; wait or stop one"},
        headers={"Retry-After": str(BUSY_RETRY_SECONDS)},
    )


@router.post("/nodes/{node_id}/run", status_code=201)
async def run_node(
    node_id: uuid.UUID,
    request: Request,
    model: str | None = Query(default=None, max_length=200),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """Start one task node in its application (Zielanwendung). The node shows in progress until
    the run ends; the result is checked against its form (project_runs.py)."""
    try:
        task = await request.app.state.project_runs.start_node(user.uuid, node_id, model=model)
    except ProgramsBusy as exc:
        raise _busy() from exc
    except RunError as exc:
        raise _run_refused(exc) from exc
    except ProjectError as exc:
        raise _refused(exc) from exc
    except AgentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc
    return agent_task_view(task)


class RequirementsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int
    items: list[str | dict[str, Any]] = Field(default_factory=list, max_length=60)


@router.put("/nodes/{node_id}/requirements")
async def put_requirements(
    node_id: uuid.UUID,
    body: RequirementsBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """What every further iteration of the task must hold (project_iterations.py): the person
    adds and strikes out; no model rewrites them."""
    try:
        node = await _iterations(request).set_requirements(
            user.uuid, node_id, body.items, revision=body.revision
        )
        project = await _store(request).get_project(user.uuid, node.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return node_view(node, project.title)


class IterateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    based_on: uuid.UUID | None = None
    feedback: str = Field(default="", max_length=4000)
    keep: bool = True  # the feedback becomes a requirement of every further iteration
    model: str | None = Field(default=None, max_length=200)
    # an issue chooses its step: find the cause (issue_check), write the fix (issue_fix) or plan
    # it as steps, one fresh run per place (issue_steps)
    program: str | None = Field(default=None, pattern="^issue_(check|fix|steps|investigate|plan)$")


@router.post("/nodes/{node_id}/iterate", status_code=201)
async def iterate_node(
    node_id: uuid.UUID,
    body: IterateBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The next iteration of a task, one click of the person (D15): it continues ``based_on`` (a
    result of the task; none: the newest) with the feedback, under the task's requirements."""
    iterations = _iterations(request)
    try:
        await iterations.begin(
            user.uuid, node_id, based_on=body.based_on, feedback=body.feedback, keep=body.keep
        )
        task = await request.app.state.project_runs.start_node(
            user.uuid, node_id, model=body.model, iterate=True, program=body.program
        )
    except ProgramsBusy as exc:
        await iterations.cancel(user.uuid, node_id)  # the begun iteration does not stay open
        raise _busy() from exc
    except RunError as exc:
        await iterations.cancel(user.uuid, node_id)
        raise _run_refused(exc) from exc
    except ProjectError as exc:
        raise _refused(exc) from exc
    except AgentError as exc:
        await iterations.cancel(user.uuid, node_id)
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc
    return {"task": str(node_id), "run": str(task.id)}


class GivenStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=300)
    # a place is a symbol (1.10) or, as before, an anchor line; fix_steps.parse_plan wants one
    anchor: str = Field(default="", max_length=2000)
    symbol: str = Field(default="", max_length=300)
    where: str = Field(default="in", max_length=10)
    do: str = Field(min_length=1, max_length=4000)
    after: list[int] = Field(default_factory=list, max_length=12)


class GivenTest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=300)
    do: str = Field(min_length=1, max_length=4000)


class GivenPlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=500)
    steps: list[GivenStep] = Field(min_length=1, max_length=12)
    test: GivenTest


@router.post("/nodes/{node_id}/plan", status_code=201)
async def give_plan(
    node_id: uuid.UUID,
    body: GivenPlanBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The person writes the plan of an issue themself ("Plan vorgeben"): the hub holds it against
    the live code, files it as the issue's result, and "Plan umsetzen" carries it out."""
    try:
        plan, problems = await request.app.state.project_runs.given_plan(
            user.uuid, node_id, body.model_dump()
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    if plan is None:
        raise HTTPException(
            422,
            detail={"code": "plan_refused", "message": "; ".join(problems), "problems": problems},
        )
    return {"plan": plan}


@router.get("/nodes/{node_id}/iterations")
async def list_iterations(
    node_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The iterations of a task, oldest first, and its requirements."""
    store = _store(request)
    try:
        node = await store.get_node(user.uuid, node_id)
        nodes = await store.tree(user.uuid, node.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    by_id = {n.id: n for n in nodes}
    return {
        "task": str(node.id),
        "title": node.title,
        "number": node.number,
        "revision": node.revision,
        "requirements": list(node.requirements or []),
        "iterating": node.next_iteration is not None,
        "iterations": [iteration_view(r, by_id) for r in results_of(nodes, node.id)],
    }


class IssueBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=3, max_length=4000)
    application: str | None = Field(default=None, max_length=40)
    conversation: str | None = Field(default=None, max_length=64)


@router.post("/issues", status_code=201)
async def file_issue(
    body: IssueBody, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The person's click on "file as issue" (project_issues.py): the task with its context."""
    from topcoat_hub import project_issues

    title = (getattr(request.app.state.settings, "issue_project", "") or "").strip()
    title = title or project_issues.DEFAULT_PROJECT
    try:
        task, alike = await project_issues.file_issue(
            _store(request),
            user.uuid,
            body.text,
            project_title=title,
            application=body.application,
            conversation=body.conversation,
            where="Karte",
        )
        project = await _store(request).get_project(user.uuid, task.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {
        "task": node_view(task, project.title),
        "similar": [{"id": str(n.id), "number": n.number, "title": n.title} for n in alike],
    }


class CheckIssuesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    max_runs: int = Field(default=10, ge=1, le=50)


@router.post("/issues/check-open", status_code=201)
async def check_open_issues(
    body: CheckIssuesBody, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Every open issue without a result yet, as one batch of ``issue_check`` runs (the person's
    approval of the batch is this click)."""
    from topcoat_hub import project_issues

    try:
        nodes = await _store(request).tree(user.uuid, body.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    wanted = [n.id for n in project_issues.select_open(nodes)]
    if not wanted:
        return {"batch": None, "count": 0}
    try:
        batch = await request.app.state.project_runs.create_batch(
            user.uuid, body.project_id, wanted, max_runs=max(body.max_runs, len(wanted))
        )
    except RunError as exc:
        raise _run_refused(exc) from exc
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"batch": batch_view(batch), "count": len(wanted)}


def _iterations(request: Request) -> IterationStore:
    return IterationStore(
        request.app.state.sessionmaker, PERSON, request.app.state.settings.hub_timezone
    )


@router.post("/projects/{project_id}/batches", status_code=201)
async def create_batch(
    project_id: uuid.UUID,
    body: BatchBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The person's approval of a batch: the ready tasks run side by side as far as the nodes of
    their applications have places; a blocked task holds back its dependants, and the limit of
    runs ends the batch."""
    try:
        batch = await request.app.state.project_runs.create_batch(
            user.uuid, project_id, body.node_ids, body.max_runs
        )
    except RunError as exc:
        raise _run_refused(exc) from exc
    except ProjectError as exc:
        raise _refused(exc) from exc
    return batch_view(batch)


@router.get("/projects/{project_id}/batches")
async def list_batches(
    project_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        rows = await request.app.state.project_runs.batches(user.uuid, project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"batches": [batch_view(b) for b in rows]}


@router.get("/batches/{batch_id}")
async def batch_state(
    batch_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The batch and the state of each of its tasks (the chat card reads it while it runs)."""
    try:
        return await request.app.state.project_runs.detail(user.uuid, batch_id)
    except ProjectError as exc:
        raise _refused(exc) from exc


@router.post("/batches/{batch_id}/stop")
async def stop_batch(
    batch_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The person's stop: no further task of the batch starts; a run going on finishes."""
    try:
        batch = await request.app.state.project_runs.stop_batch(user.uuid, batch_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return batch_view(batch)


@router.post("/nodes/{node_id}/move")
async def move_node(
    node_id: uuid.UUID,
    body: MoveBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    store = _store(request)
    try:
        node = await store.move_node(
            user.uuid, node_id, revision=body.revision, parent_id=body.parent_id
        )
        project = await store.get_project(user.uuid, node.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return node_view(node, project.title)


@router.post("/nodes/{node_id}/answer")
async def answer_question(
    node_id: uuid.UUID,
    body: AnswerBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    store = _store(request)
    try:
        node = await store.answer_question(user.uuid, node_id, body.answer)
        project = await store.get_project(user.uuid, node.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return node_view(node, project.title)


@router.post("/nodes/{node_id}/decide")
async def decide_request(
    node_id: uuid.UUID,
    body: DecideBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The person confirms (``confirm: true``) or declines a request the project agent made under
    a ``confirm`` grant."""
    store = _store(request)
    service = MessageService(store, request.app.state.agents)
    try:
        node, _ = await service.decide(user.uuid, user.is_admin, node_id, confirm=body.confirm)
        project = await store.get_project(user.uuid, node.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return node_view(node, project.title)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=4000)
    model: str | None = Field(None, max_length=255)


def _changes(request: Request) -> ChangeStore:
    return ChangeStore(request.app.state.sessionmaker)


def _change_error(exc: ChangeError) -> HTTPException:
    return HTTPException(exc.status, {"error": exc.code, "message": exc.message})


async def _readable(request: Request, user_id: uuid.UUID, view: dict[str, Any]) -> dict[str, Any]:
    """A prepared change as a person reads it: the entry it goes under by its number and title,
    not by its id (live 2026-11-17: "Darunter 44c2095b-…" under each of three new tasks). Read at
    every listing, so changes prepared before this keep up; an entry that cannot be found (not
    created yet, deleted) leaves the change as it is."""
    preview = view.get("preview") or {}
    fields = preview.get("fields") or {}
    parent = fields.get("parent_id")
    if not isinstance(parent, str):
        return view
    try:
        store = _store(request)
        node = await store.get_node(user_id, uuid.UUID(parent))
        project = await store.get_project(user_id, node.project_id)
    except (ValueError, ProjectError):
        return view
    shown = node_view(node, project.title)
    return {
        **view,
        "preview": {**preview, "parent": {"ref": shown["ref"], "title": node.title}},
    }


@router.get("/changes")
async def list_changes(
    request: Request, task_id: uuid.UUID, user: CurrentUser = Depends(api_user)
) -> list[dict[str, Any]]:
    """What the Projekte chat prepared in one check, with the state of each (pending, applied …)."""
    rows = await _changes(request).for_task(user.uuid, task_id)
    return [await _readable(request, user.uuid, change_view(r)) for r in rows]


@router.post("/changes/{change_id}/apply")
async def apply_change(
    change_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The person's click on one prepared change (D15): only the signed-in owner, never a key."""
    if user.via_key:
        raise HTTPException(403, {"error": "forbidden", "message": "only a click applies a change"})
    store = _changes(request)
    try:
        row = await store.claim(user.uuid, change_id)
    except ChangeError as exc:
        raise _change_error(exc) from exc
    caller = Caller(user.uuid, False, "gui", application=APPLICATION, task_id=row.task_id)
    try:
        done = await request.app.state.project_tools.run_tool(caller, row.capability, row.input)
    except ToolError as exc:
        failed = await store.finish(
            user.uuid, change_id, FAILED, {"error": exc.code, "message": exc.message}
        )
        return await _readable(request, user.uuid, change_view(failed))
    applied = await store.finish(user.uuid, change_id, APPLIED, done["output"])
    return await _readable(request, user.uuid, change_view(applied))


@router.post("/changes/{change_id}/refuse")
async def refuse_change(
    change_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        refused = await _changes(request).refuse(user.uuid, change_id)
        return await _readable(request, user.uuid, change_view(refused))
    except ChangeError as exc:
        raise _change_error(exc) from exc


@router.post("/projects/{project_id}/runs", status_code=201)
async def start_run(
    project_id: uuid.UUID,
    body: RunRequest,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """Ask the project agent to work on one project. The run is a task of the agent with
    application ``project``: it has the project tools and its own memory, and nothing else."""
    try:
        project = await _store(request).get_project(user.uuid, project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    # the project's id travels in the prompt: the project tools take it as an argument, and the
    # digest (built by code, titles only) is what the agent knows about the project to begin with
    zone = await user_zone(request.app.state.sessionmaker, user.uuid, _zone_of(request))
    digest = await project_digest(_store(request), user.uuid, project_id, zone=zone)
    prompt = f"[project {project.id}: {project.title}]\n{digest}\n\n{body.prompt.strip()}"
    try:
        task = await request.app.state.agents.start(
            user, prompt, body.model, application=APPLICATION
        )
    except AgentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc
    return agent_task_view(task)


@router.get("/nodes/{node_id}/history")
async def node_history(
    node_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The versions of a node's title and body, oldest first."""
    try:
        rows = await _store(request).node_history(user.uuid, node_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"versions": [version_view(v, at) for v, at in rows]}


@router.get("/nodes/{node_id}/at")
async def node_at(
    node_id: uuid.UUID,
    request: Request,
    rev: int = Query(ge=1),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The title and body of a node as they were at a revision."""
    try:
        return await _store(request).node_at(user.uuid, node_id, rev)
    except ProjectError as exc:
        raise _refused(exc) from exc


class RevertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int  # the revision the page read: a stale one is refused like any other write
    to_rev: int = Field(ge=1)  # the revision whose title and body come back


@router.post("/nodes/{node_id}/revert")
async def revert_node(
    node_id: uuid.UUID,
    body: RevertBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """Put a node's title and body back as they were at an earlier revision. It is a **new**
    revision: nothing is overwritten, the history keeps both the change and the way back."""
    store = _store(request)
    try:
        if body.to_rev > (await store.get_node(user.uuid, node_id)).revision:
            raise NotFound("version")  # a revision that never was
        then = await store.node_at(user.uuid, node_id, body.to_rev)
        node = await store.update_node(
            user.uuid, node_id, revision=body.revision, title=then["title"], body=then["body"]
        )
        project = await store.get_project(user.uuid, node.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return node_view(node, project.title)


@router.get("/nodes/{node_id}/diff")
async def node_diff(
    node_id: uuid.UUID,
    request: Request,
    from_rev: int = Query(alias="from", ge=1),
    to_rev: int = Query(alias="to", ge=1),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """What changed in a node's title and body between two revisions."""
    try:
        return await _store(request).node_diff(user.uuid, node_id, from_rev, to_rev)
    except ProjectError as exc:
        raise _refused(exc) from exc


@router.get("/akte/size")
async def akte_size(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    """How big the Akte is, for the admin: rows and bytes of the event and version tables, and
    whether they passed the soft warning level. Nothing is deleted because of it."""
    if not user.is_admin:
        raise HTTPException(403, {"error": "forbidden", "message": "only an admin sees the sizes"})
    report = await _store(request).size_report()
    limit_mb = request.app.state.settings.project_akte_warn_mb
    total = (report["events_bytes"] + report["versions_bytes"]) / (1 << 20)
    return {**report, "warn_mb": limit_mb, "warning": limit_mb > 0 and total > limit_mb}


# --- labels ---------------------------------------------------------------------------------------


class LabelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=60)
    color: str | None = Field(default=None, max_length=20)
    description: str = Field(default="", max_length=200)


class LabelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=60)
    color: str | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None, max_length=200)


class NodeLabelsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    add: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    remove: list[uuid.UUID] = Field(default_factory=list, max_length=20)


@router.get("/labels")
async def list_labels(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    labels = await _store(request).list_labels(user.uuid)
    return {"labels": [label_view(label) for label in labels]}


@router.post("/labels", status_code=201)
async def create_label(
    body: LabelBody, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        label = await _store(request).create_label(
            user.uuid, title=body.title, color=body.color, description=body.description
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    return label_view(label)


@router.patch("/labels/{label_id}")
async def update_label(
    label_id: uuid.UUID,
    body: LabelPatch,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    try:
        label = await _store(request).update_label(
            user.uuid, label_id, **body.model_dump(exclude_unset=True)
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    return label_view(label)


@router.delete("/labels/{label_id}", status_code=204)
async def delete_label(
    label_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> None:
    try:
        await _store(request).delete_label(user.uuid, label_id)
    except ProjectError as exc:
        raise _refused(exc) from exc


@router.put("/nodes/{node_id}/labels")
async def set_node_labels(
    node_id: uuid.UUID,
    body: NodeLabelsBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    try:
        labels = await _store(request).set_node_labels(
            user.uuid, node_id, add=body.add, remove=body.remove
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"labels": [label_view(label) for label in labels]}


# --- relations ------------------------------------------------------------------------------------


class RelationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    other_id: uuid.UUID
    kind: Literal["blocked_by", "related", "duplicate_of", "precedes", "copied_from", "supersedes"]


async def _relation_views(
    store: ProjectStore, user_id: uuid.UUID, node_id: uuid.UUID, ended: bool
) -> list[dict[str, Any]]:
    out = []
    titles: dict[uuid.UUID, str] = {}
    for relation, far in await store.relations_of(user_id, node_id, include_ended=ended):
        if far.project_id not in titles:
            titles[far.project_id] = (await store.get_project(user_id, far.project_id)).title
        out.append(relation_view(relation, far, titles[far.project_id], node_id))
    return out


@router.get("/nodes/{node_id}/relations")
async def read_relations(
    node_id: uuid.UUID,
    request: Request,
    ended: bool = False,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The relations of a node, also to nodes of other projects; with ``ended`` the history too."""
    try:
        return {"relations": await _relation_views(_store(request), user.uuid, node_id, ended)}
    except ProjectError as exc:
        raise _refused(exc) from exc


@router.post("/nodes/{node_id}/relations", status_code=201)
async def add_relation(
    node_id: uuid.UUID,
    body: RelationBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    store = _store(request)
    try:
        await store.add_relation(user.uuid, node_id, body.other_id, body.kind)
        return {"relations": await _relation_views(store, user.uuid, node_id, False)}
    except ProjectError as exc:
        raise _refused(exc) from exc


@router.post("/relations/{relation_id}/end")
async def end_relation(
    relation_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        relation = await _store(request).end_relation(user.uuid, relation_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"id": str(relation.id), "valid_to": relation.valid_to.isoformat()}


# --- reminders ------------------------------------------------------------------------------------


class ReminderBody(BaseModel):
    """Either ``at`` (a time with an offset) or ``relative_to`` (due, start, end) with an
    ``offset_seconds`` (negative = before)."""

    model_config = ConfigDict(extra="forbid")

    at: datetime | None = None
    relative_to: Literal["due", "start", "end"] | None = None
    offset_seconds: int | None = None


@router.get("/nodes/{node_id}/reminders")
async def read_reminders(
    node_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        rows = await _store(request).reminders_of(user.uuid, node_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"reminders": [reminder_view(r) for r in rows]}


@router.post("/nodes/{node_id}/reminders", status_code=201)
async def add_reminder(
    node_id: uuid.UUID,
    body: ReminderBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    try:
        reminder = await _store(request).add_reminder(
            user.uuid,
            node_id,
            at=body.at,
            relative_to=body.relative_to,
            offset_seconds=body.offset_seconds,
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    return reminder_view(reminder)


@router.delete("/reminders/{reminder_id}", status_code=204)
async def remove_reminder(
    reminder_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> None:
    try:
        await _store(request).remove_reminder(user.uuid, reminder_id)
    except ProjectError as exc:
        raise _refused(exc) from exc


# --- quiet hours and the daily digest of the person -----------------------------------------------


class NotificationPrefs(BaseModel):
    """Local times of day (``HH:MM``, the person's time zone); the span may cross midnight."""

    model_config = ConfigDict(extra="forbid")

    quiet_start: str | None = None
    quiet_end: str | None = None
    digest_hour: int | None = Field(default=None, ge=0, le=23)


@router.get("/notification-settings")
async def read_notification_settings(
    request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    return await read_prefs(request.app.state.sessionmaker, user.uuid)


@router.put("/notification-settings")
async def put_notification_settings(
    body: NotificationPrefs, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Quiet hours hold reminders and the digest back; the digest hour chooses when the daily list
    of overdue tasks may reach the inbox (none = no digest)."""
    try:
        return await write_prefs(
            request.app.state.sessionmaker,
            user.uuid,
            quiet_start=body.quiet_start,
            quiet_end=body.quiet_end,
            digest_hour=body.digest_hour,
        )
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "invalid", "message": str(exc)}) from exc


# --- reports that replace each other --------------------------------------------------------------


class SupersedeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by_id: uuid.UUID


@router.post("/nodes/{node_id}/supersede")
async def supersede_report(
    node_id: uuid.UUID,
    body: SupersedeBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """``node_id`` is replaced by the report ``by_id``: it stays, no longer current."""
    store = _store(request)
    try:
        new = await store.supersede_report(user.uuid, node_id, by_id=body.by_id)
        project = await store.get_project(user.uuid, new.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return node_view(new, project.title)


# --- the trash: by hand, never by the agent
# --------------------------------------------------------


class PurgeBody(BaseModel):
    """Delete for good: type the title; ``keep_timeline`` keeps the lines of the Akte (without their
    texts), ``remove_everything`` (a project only) removes them as well."""

    model_config = ConfigDict(extra="forbid")

    confirm_title: str = Field(min_length=1, max_length=200)
    mode: Literal["keep_timeline", "remove_everything"] = "keep_timeline"


def _only_the_person(user: CurrentUser) -> None:
    """The trash is the person's own hand: not an API key, which an agent could hold."""
    if user.via_key:
        raise HTTPException(
            403, {"error": "forbidden", "message": "only the person empties the trash"}
        )


@router.get("/trash")
async def read_trash(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    projects, nodes = await _store(request).list_trash(user.uuid)
    return {
        "projects": [project_view(p) for p in projects],
        "nodes": [node_view(n, p.title) | {"project_title": p.title} for n, p in nodes],
    }


@router.post("/projects/{project_id}/trash")
async def trash_project(
    project_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        return project_view(await _store(request).trash_project(user.uuid, project_id))
    except ProjectError as exc:
        raise _refused(exc) from exc


@router.post("/projects/{project_id}/restore")
async def restore_project(
    project_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        return project_view(await _store(request).restore_project(user.uuid, project_id))
    except ProjectError as exc:
        raise _refused(exc) from exc


@router.post("/projects/{project_id}/purge", status_code=204)
async def purge_project(
    project_id: uuid.UUID,
    body: PurgeBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> None:
    _only_the_person(user)
    try:
        await _store(request).purge_project(
            user.uuid, project_id, confirm_title=body.confirm_title, mode=body.mode
        )
    except ProjectError as exc:
        raise _refused(exc) from exc


@router.post("/nodes/{node_id}/trash")
async def trash_node(
    node_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    store = _store(request)
    try:
        node = await store.trash_node(user.uuid, node_id)
        project = await store.get_project(user.uuid, node.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return node_view(node, project.title)


@router.post("/nodes/{node_id}/restore")
async def restore_node(
    node_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    store = _store(request)
    try:
        node = await store.restore_node(user.uuid, node_id)
        project = await store.get_project(user.uuid, node.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return node_view(node, project.title)


@router.post("/nodes/{node_id}/purge", status_code=204)
async def purge_node(
    node_id: uuid.UUID,
    body: PurgeBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> None:
    _only_the_person(user)
    if body.mode != "keep_timeline":
        raise HTTPException(
            422, {"code": "invalid", "message": "a node is deleted keeping the timeline"}
        )
    try:
        await _store(request).purge_node(user.uuid, node_id, confirm_title=body.confirm_title)
    except ProjectError as exc:
        raise _refused(exc) from exc


# --- search, the filter help, the digest, saved views ---------------------------------------------


def _zone_of(request: Request) -> Any:
    return request.app.state.settings.hub_timezone


@router.get("/nodes")
async def search_nodes(
    request: Request,
    filter_text: str = Query("", alias="filter", max_length=1000),
    sort: str = Query("", max_length=200),
    project: uuid.UUID | None = None,
    limit: int = Query(25, ge=1, le=200),
    after: int = Query(0, ge=0),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """Nodes across the person's projects: ``filter`` is the query language of the help route,
    ``sort`` a rule like ``dueDate:asc,priority:desc``."""
    store = _store(request)
    zone = await user_zone(request.app.state.sessionmaker, user.uuid, _zone_of(request))
    try:
        found = await store.search_nodes(
            user.uuid,
            filter_text=filter_text,
            sort=sort,
            project_id=project,
            limit=limit,
            after=after,
            zone=zone,
        )
    except (FilterError, SortError) as exc:
        raise HTTPException(422, detail={"code": "invalid", "message": str(exc)}) from exc
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {
        "nodes": [
            node_view(n, title, progress=done, labels=labels) | {"project_title": title}
            for n, title, done, labels in found.rows
        ],
        "total": found.total,
        "next_after": found.next_after,
    }


@router.get("/filter/help")
async def filter_help(user: CurrentUser = Depends(api_user)) -> dict[str, str]:
    return {"help": help_text()}


@router.get("/projects/{project_id}/digest")
async def read_digest(
    project_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, str]:
    """What the project agent has in its context about a project."""
    zone = await user_zone(request.app.state.sessionmaker, user.uuid, _zone_of(request))
    try:
        return {"digest": await project_digest(_store(request), user.uuid, project_id, zone=zone)}
    except ProjectError as exc:
        raise _refused(exc) from exc


class ViewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=80)
    type: Literal["list", "table", "board", "calendar", "gantt"] = "list"
    filter: str = Field(default="", max_length=1000)
    sort: str = Field(default="", max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    project_id: uuid.UUID | None = None
    favorite: bool = False


class ViewPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int
    title: str | None = Field(default=None, min_length=1, max_length=80)
    type: Literal["list", "table", "board", "calendar", "gantt"] | None = None
    filter: str | None = Field(default=None, max_length=1000)
    sort: str | None = Field(default=None, max_length=200)
    config: dict[str, Any] | None = None
    favorite: bool | None = None
    position: int | None = None


def _view_out(v: Any) -> dict[str, Any]:
    return {
        "id": str(v.id),
        "project_id": str(v.project_id) if v.project_id else None,
        "title": v.title,
        "type": v.type,
        "filter": v.filter,
        "sort": v.sort,
        "config": v.config,
        "favorite": v.favorite,
        "position": v.position,
        "builtin": v.builtin,
        "revision": v.revision,
    }


@router.get("/views")
async def list_views(
    request: Request, project: uuid.UUID | None = None, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        views = await _store(request).list_views(user.uuid, project)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"views": [_view_out(v) for v in views]}


@router.post("/views", status_code=201)
async def create_view(
    body: ViewBody, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        view = await _store(request).create_view(user.uuid, **body.model_dump())
    except ProjectError as exc:
        raise _refused(exc) from exc
    return _view_out(view)


@router.patch("/views/{view_id}")
async def update_view(
    view_id: uuid.UUID, body: ViewPatch, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True, exclude_none=True, exclude={"revision"})
    try:
        view = await _store(request).update_view(
            user.uuid, view_id, revision=body.revision, **changes
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    return _view_out(view)


@router.delete("/views/{view_id}", status_code=204)
async def delete_view(
    view_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> None:
    try:
        await _store(request).delete_view(user.uuid, view_id)
    except ProjectError as exc:
        raise _refused(exc) from exc


# --- change sets: the agent composes, the person applies with one click ---------------------------


class ApplyBody(BaseModel):
    """``selection``: the items (0-based) that stay ticked; none = all of them."""

    model_config = ConfigDict(extra="forbid")

    selection: list[int] | None = Field(default=None, max_length=100)


def _sets(request: Request) -> ChangeSetStore:
    return ChangeSetStore(request.app.state.sessionmaker, request.app.state.settings.hub_timezone)


def _set_error(exc: ChangeSetError) -> HTTPException:
    return HTTPException(exc.status, {"error": exc.code, "message": exc.message})


@router.get("/changesets")
async def list_change_sets(
    request: Request,
    task_id: uuid.UUID | None = None,
    project: uuid.UUID | None = None,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The change sets of a check (``task_id``) or of a project."""
    if (task_id is None) == (project is None):
        raise HTTPException(422, {"error": "invalid", "message": "name a task_id or a project"})
    store = _sets(request)
    rows = (
        await store.for_task(user.uuid, task_id)
        if task_id is not None
        else await store.for_project(user.uuid, project)  # type: ignore[arg-type]
    )
    return {"change_sets": [changeset_view(r) for r in rows]}


@router.get("/changesets/{set_id}")
async def read_change_set(
    set_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        return changeset_view(await _sets(request).get(user.uuid, set_id))
    except ChangeSetError as exc:
        raise _set_error(exc) from exc


@router.post("/changesets/{set_id}/apply")
async def apply_change_set(
    set_id: uuid.UUID,
    body: ApplyBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The person's one click on a whole set (D15, D83): only the signed-in owner, never a key."""
    if user.via_key:
        raise HTTPException(
            403, {"error": "forbidden", "message": "only a click applies a change set"}
        )
    try:
        return changeset_view(await _sets(request).apply(user.uuid, set_id, body.selection))
    except ChangeSetError as exc:
        raise _set_error(exc) from exc


@router.post("/changesets/{set_id}/refuse")
async def refuse_change_set(
    set_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    if user.via_key:
        raise HTTPException(
            403, {"error": "forbidden", "message": "only a click decides a change set"}
        )
    try:
        return changeset_view(await _sets(request).refuse(user.uuid, set_id))
    except ChangeSetError as exc:
        raise _set_error(exc) from exc


# --- fixes of issues (issue_fixes.py, D103, D104) -------------------------------------------------


def _fix_refused(exc: Any) -> HTTPException:
    return HTTPException(exc.status, detail={"code": exc.code, "message": exc.message})


@router.get("/nodes/{node_id}/fixes")
async def list_fixes(
    node_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    from topcoat_hub.issue_fixes import fix_view

    fixes = request.app.state.fixes
    await fixes.sync_rollouts()  # an outcome the host wrote while the hub restarted
    # the settled plan of the investigation, which "Plan umsetzen" carries out in steps
    plan = await request.app.state.project_runs.issue_plan(user.uuid, node_id)
    # the person's probe mandate: whether the investigation may try (issue_probes.py, D83)
    from topcoat_hub.issue_fixes import LIVE_REPO
    from topcoat_hub.issue_probes import ACTION, probe_branch, probe_scope
    from topcoat_hub.project_mandates import mandate_view

    store = _store(request)
    probes = None
    scope = ""
    try:
        issue = await store.get_node(user.uuid, node_id)
        scope = probe_scope(issue.number)  # what the card shows before the click, word for word
        found = await store.find_mandate(
            user.uuid, issue.project_id, LIVE_REPO, probe_branch(issue.number), ACTION
        )
        probes = mandate_view(found) if found is not None else None
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {
        "fixes": [fix_view(f) for f in await fixes.of_issue(user.uuid, node_id)],
        "plan": plan,
        "probe_mandate": probes,
        "probe_scope": scope,
    }


@router.get("/fixes")
async def live_fixes(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    """The fixes running on the hub, with their diff (scripts/dev/pull-fixes.py brings them into
    the maintainer's checkout before the next deploy from there)."""
    from topcoat_hub.issue_fixes import fix_view

    fixes = request.app.state.fixes
    await fixes.sync_rollouts()
    out = []
    store = _store(request)
    for f in await fixes.live_fixes(user.uuid):
        item = fix_view(f)
        with contextlib.suppress(ProjectError):
            issue = await store.get_node(user.uuid, f.issue_id)
            item["issue"] = {"number": issue.number, "title": issue.title}
        out.append(item)
    return {"fixes": out}


@router.post("/fixes/{fix_id}/apply")
async def apply_fix(
    fix_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The one click on the whole fix (D103): applied on a branch of the workshop, committed,
    tested. Only the signed-in admin, never an API key."""
    from topcoat_hub.issue_fixes import FixError, fix_view
    from topcoat_hub.tools import Caller

    try:
        fix = await request.app.state.fixes.apply(Caller.of(user), fix_id)
    except FixError as exc:
        raise _fix_refused(exc) from exc
    return fix_view(fix)


@router.post("/fixes/{fix_id}/rollout")
async def rollout_fix(
    fix_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The second click (D104): a passed fix goes to the hub host's roll-out."""
    from topcoat_hub.issue_fixes import FixError, fix_view
    from topcoat_hub.tools import Caller

    try:
        fix = await request.app.state.fixes.rollout(Caller.of(user), fix_id)
    except FixError as exc:
        raise _fix_refused(exc) from exc
    return fix_view(fix)


@router.post("/fixes/{fix_id}/discard")
async def discard_fix(
    fix_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    from topcoat_hub.issue_fixes import FixError, fix_view

    try:
        fix = await request.app.state.fixes.discard(user.uuid, fix_id)
    except FixError as exc:
        raise _fix_refused(exc) from exc
    return fix_view(fix)


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    works: bool
    feedback: str = Field(default="", max_length=4000)


@router.post("/nodes/{node_id}/confirm")
async def confirm_issue(
    node_id: uuid.UUID, body: ConfirmBody, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """After a fix runs: "it works" closes the issue; "it does not" starts the next fix iteration
    with what still fails."""
    store = _store(request)
    try:
        node = await store.get_node(user.uuid, node_id)
        if body.works:
            node = await store.update_node(
                user.uuid, node_id, revision=node.revision, status="done"
            )
            project = await store.get_project(user.uuid, node.project_id)
            return {"task": node_view(node, project.title), "run": None}
        feedback = body.feedback.strip() or "Der Fix wirkt im Betrieb nicht."
        await _iterations(request).begin(
            user.uuid, node_id, based_on=None, feedback=feedback, keep=True
        )
        program = await request.app.state.fixes.last_program(user.uuid, node_id)
        if program == "issue_plan":  # a settled plan that does not work: investigate again
            program = "issue_investigate"
        task = await request.app.state.project_runs.start_node(
            user.uuid, node_id, iterate=True, program=program
        )
    except ProgramsBusy as exc:
        await _iterations(request).cancel(user.uuid, node_id)
        raise _busy() from exc
    except RunError as exc:
        await _iterations(request).cancel(user.uuid, node_id)
        raise _run_refused(exc) from exc
    except ProjectError as exc:
        raise _refused(exc) from exc
    project = await store.get_project(user.uuid, node.project_id)
    node = await store.get_node(user.uuid, node_id)
    return {"task": node_view(node, project.title), "run": str(task.id)}
