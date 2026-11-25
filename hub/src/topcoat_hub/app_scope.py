"""The tools of an application are a ceiling, not a starting point (docs/PLATFORM.md, 3.1).

A conversation belongs to an application (Chat, Research, Development, Administration). What the
application's workspace allows, narrowed by the person's profile, is the most the conversation may
use: the person can switch tools off in a conversation, never on beyond the ceiling. The ceiling is
applied by the hub where a turn's scope is settled, so it holds whatever the browser sends. The
conversations from before applications were given to Chat (migration 0063); a conversation a
program opens through the API without naming an application has no ceiling beyond the person's
grants (D20), which always apply. An application the person may not see has no tools at all."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import memory_scope
from topcoat_hub.auth import CurrentUser
from topcoat_hub.ui.access import effective_agent, resolve_access
from topcoat_hub.ui.store import TemplateStore

# what an application nobody may see leaves: nothing but its own words
NOTHING: dict[str, Any] = {"nodes": None, "groups": [], "internet": False}


async def ceiling(db: AsyncSession, user: CurrentUser, application: str | None) -> dict[str, Any]:
    """The most a conversation of ``application`` may use, in the shape of a tool scope
    (``groups`` None = every group the person may use; ``internet`` None = allowed)."""
    if application is None:
        return {"nodes": None, "groups": None, "internet": None}
    key = memory_scope.clean_application(application)
    template = None if key is None else await TemplateStore(db).get(key)
    access = await resolve_access(db, user.uuid, user.role)
    if template is None or not access.workspace_visible(template):
        return dict(NOTHING)
    scope = effective_agent(template.agent, access)["tool_scope"]
    if scope is None:
        return {"nodes": None, "groups": None, "internet": None}
    return {
        "nodes": scope.get("nodes"),
        "groups": scope.get("groups"),
        "internet": scope.get("internet"),
    }


def cap(scope: dict[str, Any], limit: dict[str, Any]) -> dict[str, Any]:
    """``scope`` (a settled turn scope, :func:`tool_groups.resolve_scope`) narrowed to ``limit``:
    the groups both allow, the nodes both allow, the internet only when both have it on. Nothing
    the scope switched off comes back."""
    out = dict(scope)
    want, allow = scope.get("groups"), limit.get("groups")
    if allow is not None:
        out["groups"] = [g for g in allow if want is None or g in want]
    want_nodes, allow_nodes = scope.get("nodes"), limit.get("nodes")
    if allow_nodes is not None:
        out["nodes"] = [n for n in allow_nodes if want_nodes is None or n in want_nodes]
    if limit.get("internet") is False:
        out["internet"] = False
    return out


def groups_within(keys: list[str], limit: dict[str, Any]) -> list[str]:
    """The tool groups of ``keys`` an application allows (for the picker)."""
    allow = limit.get("groups")
    return keys if allow is None else [k for k in keys if k in allow]


def internet_allowed(limit: dict[str, Any]) -> bool:
    return limit.get("internet") is not False
