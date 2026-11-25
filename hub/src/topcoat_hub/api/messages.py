"""Messages and chains of the Projekte application (PPH-05).

Same authorization as the other project routes: a signed-in session, or an API key under the scoping
it has there. Every query is scoped to the caller; another person's project, chain or message is a
404. Sending is not here: a message is made by the agents' tools and by the hub when something
arrives (``message_service.py``).
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from topcoat_hub import messages
from topcoat_hub.api.project import _refused, _store
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.message_service import MessageService
from topcoat_hub.project_messages import chain_view
from topcoat_hub.project_store import ProjectError
from topcoat_hub.project_views import node_view

router = APIRouter(prefix="/api/project")


@router.get("/messages/registry")
async def message_registry(user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    """The types, who may send what to whom, and what each application is responsible for: all the
    page and the agents need to know about each other."""
    reg = messages.load()
    return {
        "types": [
            {
                "type": t.name,
                "from": list(t.senders),
                "to": list(t.receivers),
                "trust": t.trust,
                "needs_review": t.needs_review,
                "effect": t.effect,
                "label": t.labels,
            }
            for t in reg.types.values()
        ],
        "responsibilities": reg.responsibilities,
        "states": list(reg.states),
    }


@router.get("/projects/{project_id}/messages")
async def read_messages(
    project_id: uuid.UUID,
    request: Request,
    chain: uuid.UUID | None = None,
    type: str | None = Query(default=None, max_length=40),
    state: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=100, ge=1, le=500),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The messages of a project, oldest first: by chain, type or state."""
    if type is not None and type not in messages.load().types:
        raise HTTPException(422, {"code": "invalid", "message": f"unknown message type: {type}"})
    store = _store(request)
    try:
        project = await store.get_project(user.uuid, project_id)
        rows = await store.messages_of(
            user.uuid, project_id, chain_id=chain, type=type, state=state, limit=limit
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"messages": [node_view(n, project.title) for n in rows]}


@router.get("/projects/{project_id}/chains")
async def read_chains(
    project_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    store = _store(request)
    try:
        await store.get_project(user.uuid, project_id)
        rows = await store.chains_of(user.uuid, project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"chains": [chain_view(c) for c in rows]}


@router.get("/chains/{chain_id}")
async def read_chain(
    chain_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """One chain with its messages in order."""
    store = _store(request)
    try:
        chain = await store.get_chain(user.uuid, chain_id)
        project = await store.get_project(user.uuid, chain.project_id)
        rows = await store.messages_of(user.uuid, chain.project_id, chain_id=chain.id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"chain": chain_view(chain), "messages": [node_view(n, project.title) for n in rows]}


@router.post("/chains/{chain_id}/stop")
async def stop_chain(
    chain_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The person's stop for a whole conversation: what is on its way is stopped, its agent tasks
    are canceled, and the chain takes no new message. Stopping twice changes nothing."""
    store = _store(request)
    try:
        stopped = await MessageService(store, request.app.state.agents).stop_chain(
            user.uuid, chain_id
        )
        chain = await store.get_chain(user.uuid, chain_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"chain": chain_view(chain), "stopped": [str(n.id) for n in stopped]}


class ProposalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accept: bool


@router.post("/nodes/{node_id}/proposal")
async def decide_proposal(
    node_id: uuid.UUID,
    body: ProposalDecision,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The person's click on the card of a proposal an application made on its own: accept makes a
    task of it, decline closes it. A key (a program) cannot click."""
    if user.via_key:
        raise HTTPException(
            403, {"error": "person_only", "message": "only the signed-in person decides"}
        )
    store = _store(request)
    try:
        node = await MessageService(store, request.app.state.agents).decide_proposal(
            user.uuid, node_id, accept=body.accept
        )
        project = await store.get_project(user.uuid, node.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return node_view(node, project.title)
