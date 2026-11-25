"""Mandates of the Projekte application (PPH-05, 05d).

A mandate is made only here, by the signed-in person's click on the card of an order: not by an API
key, not by an agent, and only for an admin for now. The scope comes from the order, never from the
request body; the body only says how long it runs, in the person's own words, and whether the hub
may push the branch. Revoking is the person's click too. Nothing else creates or changes one.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from topcoat_hub.api.project import _refused, _store
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.message_service import MessageService
from topcoat_hub.project_mandates import DEFAULT_DAYS, MAX_DAYS, mandate_view
from topcoat_hub.project_store import ProjectError
from topcoat_hub.project_views import node_view

router = APIRouter(prefix="/api/project")


class ReleaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(DEFAULT_DAYS, ge=1, le=MAX_DAYS)
    text: str = Field("", max_length=2000)
    push: bool = True


def _person(user: CurrentUser) -> None:
    if user.via_key or not user.is_admin:
        raise HTTPException(
            403, {"error": "person_only", "message": "only the signed-in admin gives a mandate"}
        )


@router.post("/nodes/{order_id}/mandate")
async def release_order(
    order_id: uuid.UUID,
    body: ReleaseBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """Release an order that waits and give the mandate for its repository and branch."""
    _person(user)
    store = _store(request)
    service = MessageService(store, request.app.state.agents, request.app.state.proposals)
    try:
        order, mandate, note = await service.release_order(
            user.uuid, True, order_id, days=body.days, text=body.text, push=body.push
        )
        project = await store.get_project(user.uuid, order.project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {
        "order": node_view(order, project.title),
        "mandate": mandate_view(mandate),
        "queue_note": note,
    }


@router.post("/nodes/{issue_id}/probe-mandate")
async def allow_probes(
    issue_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The person's click on the card "Versuche erlauben" of an issue: the probe mandate of its
    investigation (issue_probes.py, D83). The scope is the hub's, fixed by the issue: the request
    names nothing but the issue."""
    from topcoat_hub.issue_fixes import LIVE_REPO
    from topcoat_hub.issue_probes import ACTION, DAYS, probe_branch, probe_scope

    _person(user)
    store = _store(request)
    try:
        issue = await store.get_node(user.uuid, issue_id)
        if issue.kind != "task":
            raise HTTPException(409, {"error": "not_issue", "message": "only an issue has tries"})
        mandate = await store.create_mandate(
            user.uuid,
            issue.project_id,
            repo=LIVE_REPO,
            branch=probe_branch(issue.number),
            actions=[ACTION],
            push=False,
            text=probe_scope(issue.number),
            days=DAYS,
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"mandate": mandate_view(mandate)}


@router.get("/projects/{project_id}/mandates")
async def read_mandates(
    project_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    store = _store(request)
    try:
        rows = await store.mandates_of(user.uuid, project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"mandates": [mandate_view(m) for m in rows]}


@router.post("/mandates/{mandate_id}/revoke")
async def revoke_mandate(
    mandate_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Take a mandate back. Orders that have not left yet wait for a click again."""
    _person(user)
    store = _store(request)
    try:
        mandate = await store.revoke_mandate(user.uuid, mandate_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return {"mandate": mandate_view(mandate)}
