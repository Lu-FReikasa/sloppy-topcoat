"""Registered repositories (E6-01 S0, D52, D76): external ones (add, refresh, remove) and local
ones in the workshop of a dev node (create, clone from an external one, branches, take a branch
into the default branch, publish, pull, connect, remove). Admin only (the dev token, never an API
key). A token goes in once and is never returned."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from topcoat_hub.auth import CurrentUser, require_admin
from topcoat_hub.devrepos import MODES, TEMPLATES, DevRepoStore, RepoError, view
from topcoat_hub.proposals import ProposalError
from topcoat_hub.tools import Caller

router = APIRouter(prefix="/api/dev/repos")


class AddRepo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    repo: str = Field(min_length=3, max_length=250)
    token: str = Field(min_length=1, max_length=500)
    mode: str = "read"
    test_template: str = "none"


class EditRepo(BaseModel):
    """What may change on a registered repository; what is left out stays as it is."""

    model_config = ConfigDict(extra="forbid")

    mode: str | None = None
    test_template: str | None = None
    token: str | None = Field(default=None, max_length=500)


class PushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=3, max_length=2000)
    node_id: uuid.UUID | None = None


def _store(request: Request) -> DevRepoStore:
    store = getattr(request.app.state, "devrepos", None)
    if store is None:
        raise HTTPException(503, detail={"code": "unavailable", "message": "not ready"})
    return store  # type: ignore[no-any-return]


def _fail(exc: RepoError) -> HTTPException:
    return HTTPException(exc.status, detail={"code": exc.code, "message": exc.message})


@router.get("")
async def list_repos(
    request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    store = _store(request)
    rows = await store.list(uuid.UUID(user.id))
    nodes = await store.dev_nodes()
    sessions = getattr(request.app.state, "sessions", None)
    online = set(sessions.connected_ids()) if sessions is not None else set()
    names = {n.id: n.name for n in nodes}
    out = []
    for r in rows:
        item = view(r)
        item["node"] = names.get(r.node_id) if r.node_id else None
        # an external repository names its local clones; a local one has its upstream_id
        item["clones"] = [str(c.id) for c in rows if c.upstream_id == r.id]
        out.append(item)
    return {
        "repos": out,
        "modes": list(MODES),
        "templates": list(TEMPLATES),
        "nodes": [{"id": str(n.id), "name": n.name, "online": n.id in online} for n in nodes],
    }


@router.post("", status_code=201)
async def add_repo(
    body: AddRepo, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        row = await _store(request).add(
            uuid.UUID(user.id),
            name=body.name,
            repo=body.repo,
            token=body.token,
            mode=body.mode,
            template=body.test_template,
        )
    except RepoError as exc:
        raise _fail(exc) from exc
    return view(row)


@router.patch("/{ref}")
async def edit_repo(
    ref: str, body: EditRepo, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        row = await _store(request).update(
            uuid.UUID(user.id),
            ref,
            mode=body.mode,
            template=body.test_template,
            token=body.token,
        )
    except RepoError as exc:
        raise _fail(exc) from exc
    return view(row)


@router.post("/{ref}/refresh")
async def refresh_repo(
    ref: str, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        return view(await _store(request).refresh(uuid.UUID(user.id), ref))
    except RepoError as exc:
        raise _fail(exc) from exc


@router.delete("/{ref}", status_code=204)
async def remove_repo(
    ref: str, request: Request, user: CurrentUser = Depends(require_admin)
) -> None:
    """An external repository is forgotten here; a local one is deleted in its workshop too."""
    store = _store(request)
    try:
        row = await store.find(uuid.UUID(user.id), ref)
        if row is not None and row.kind == "local":
            await store.remove_local(Caller.of(user), ref)
        else:
            await store.remove(uuid.UUID(user.id), ref)
    except RepoError as exc:
        raise _fail(exc) from exc


# --- local repositories (D76) --------------------------------------------------------------


class CreateLocal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=63)
    node_id: uuid.UUID | None = None
    branch: str = Field(default="main", min_length=1, max_length=100)
    test_template: str = "none"
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)


class CloneLocal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=63)
    node_id: uuid.UUID | None = None


class NewBranch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1, max_length=100)
    start: str = Field(default="", max_length=100, alias="from")


class BranchStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch: str = Field(default="", max_length=100)
    message: str = Field(default="", max_length=2000)


class Upstream(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream: str | None = Field(default=None, max_length=200)


@router.post("/local", status_code=201)
async def create_local(
    body: CreateLocal, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    """A new repository in the workshop of a dev node, with a first commit."""
    try:
        row = await _store(request).create_local(
            Caller.of(user),
            name=body.name,
            node_id=body.node_id,
            branch=body.branch,
            template=body.test_template,
            title=body.title,
            description=body.description,
        )
    except RepoError as exc:
        raise _fail(exc) from exc
    return view(row)


@router.post("/{ref}/clone", status_code=201)
async def clone_local(
    ref: str, body: CloneLocal, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    """A local clone of an external repository, to edit and test it in the workshop."""
    try:
        row = await _store(request).clone_local(
            Caller.of(user), ref, name=body.name, node_id=body.node_id
        )
    except RepoError as exc:
        raise _fail(exc) from exc
    return view(row)


@router.get("/{ref}/branches")
async def branches(
    ref: str, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        return await _store(request).branches(Caller.of(user), ref)
    except RepoError as exc:
        raise _fail(exc) from exc


@router.post("/{ref}/branches", status_code=201)
async def create_branch(
    ref: str, body: NewBranch, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        return await _store(request).create_branch(Caller.of(user), ref, body.name, body.start)
    except RepoError as exc:
        raise _fail(exc) from exc


@router.post("/{ref}/merge")
async def merge_branch(
    ref: str, body: BranchStep, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    """Take a branch into the default branch: the click after seeing the change."""
    if not body.branch:
        raise HTTPException(422, detail={"code": "branch", "message": "name the branch"})
    try:
        return await _store(request).merge(Caller.of(user), ref, body.branch, body.message)
    except RepoError as exc:
        raise _fail(exc) from exc


@router.post("/{ref}/publish")
async def publish_branch(
    ref: str, body: BranchStep, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    """Push a branch to the connected remote (never forced)."""
    if not body.branch:
        raise HTTPException(422, detail={"code": "branch", "message": "name the branch"})
    try:
        return await _store(request).publish(Caller.of(user), ref, body.branch)
    except RepoError as exc:
        raise _fail(exc) from exc


@router.post("/{ref}/pull")
async def pull_branch(
    ref: str, body: BranchStep, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    """Bring a branch up to date with the connected remote (fast-forward only)."""
    try:
        return await _store(request).pull(Caller.of(user), ref, body.branch)
    except RepoError as exc:
        raise _fail(exc) from exc


@router.put("/{ref}/upstream")
async def set_upstream(
    ref: str, body: Upstream, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    """Connect a local repository to an external one (where it is published), or cut it off."""
    try:
        return view(await _store(request).connect(Caller.of(user), ref, body.upstream))
    except RepoError as exc:
        raise _fail(exc) from exc


@router.post("/{ref}/push-proposal")
async def push_proposal(
    ref: str, body: PushRequest, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    """Step one of a push: the hub reads the workshop's diff and stores a proposal that shows it.
    Nothing leaves the workshop here; the click on ``/api/proposals/{id}/execute`` does that."""
    try:
        return await request.app.state.proposals.create_push(
            Caller.of(user), ref, body.message, body.node_id
        )
    except ProposalError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc
