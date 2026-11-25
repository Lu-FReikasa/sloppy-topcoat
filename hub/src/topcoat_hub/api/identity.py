"""The identity view over REST (E3-12): who is in which group, which group opens which app.

Read-only. Who may look is the right to run ``keycloak.snapshot`` on a node (D20: an admin on the
GUI, anybody else by grant, never an API key by inheritance); a person who may run it nowhere sees
no source at all. The one thing an admin can write is the hub's own note of which groups an app
reads by itself (``app-groups``) — it changes nothing in Keycloak or in the app."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from topcoat_hub.auth import CurrentUser, api_user, require_admin
from topcoat_hub.identity import IdentityService, explain
from topcoat_hub.tools import Caller

router = APIRouter(prefix="/api/identity")


def _identity(request: Request) -> IdentityService:
    return request.app.state.identity


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


class AppGroupsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_groups: dict[str, list[str]] = Field(default_factory=dict, max_length=50)


@router.get("")
async def identity_view(
    request: Request,
    refresh: bool = Query(False, description="read the node now instead of using a young look"),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """Every source node the caller may use, each with its own look: people, groups, apps, the
    chain between them, the age of the look and what the look cannot see."""
    return await _identity(request).view(Caller.of(user), refresh)


@router.get("/explain")
async def identity_explain(
    request: Request,
    person: str = Query(min_length=1, max_length=100),
    app: str = Query(min_length=1, max_length=100),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """Why can this person use this app (or why not, as far as the look shows), per source."""
    views = (await _identity(request).view(Caller.of(user)))["sources"]
    return {
        "results": [
            {"node": v["node"]["name"], **explain(v, person, app)}
            for v in views
            if "error" not in v
        ]
    }


@router.put("/app-groups")
async def put_app_groups(
    body: AppGroupsRequest, request: Request, _admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    """The admin's note: which Keycloak groups an app reads by itself (a group claim). It adds
    "configured by hand" paths to the chain; it is not read from the app."""
    return {"app_groups": await _identity(request).set_app_groups(body.app_groups)}
