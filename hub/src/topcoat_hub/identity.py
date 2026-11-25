"""The identity view: who is in which group, which group opens which app (E3-12, D47).

Read-only from end to end. A tool node that has a ``keycloak`` section in its connectors file
answers one capability, ``keycloak.snapshot`` (users, groups with their members and role mappings,
clients = apps with their roles). The hub reads it through the normal capability path (the node's
config, read-only, schema, the caller's right to run it, the audit row) and computes the chain
that is otherwise a relay race through several tools:

    person  ->  group (also the parent groups: a subgroup inherits their roles)  ->  role of an app

Honesty rules, because "who may use what" is exactly the answer people act on:

* The answer says how old the look is and which node it came from; a source that failed is
  reported, never shown as "nobody".
* What Keycloak's role mappings can show is shown; what it cannot is *named as a limit* in the
  answer: composite roles are not expanded, a realm role is not an app, an app that reads a
  group from the token itself (a group claim) is only known where the admin said so (the
  "app groups" setting) — and then it is marked "configured by hand", not "seen".
* Nothing here changes anything. Access *grants* that write through to the backends are a later
  step with the proposal/confirmation rules of D15/D43.
"""

import logging
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.chat_store import get_hub_setting, set_hub_setting
from topcoat_hub.config import Settings
from topcoat_hub.tools import Caller, ToolError, ToolService

log = logging.getLogger(__name__)

CAPABILITY = "keycloak.snapshot"
QUERY_CAPABILITY = "identity.query"
IDENTITY_NODE = "identity"
IDENTITY_NODE_ID = uuid.UUID("00000000-0000-4000-8000-00000000a0d2")
# the admin's note of which Keycloak groups an app reads by itself (a group claim): hub_settings
APP_GROUPS_KEY = "identity_app_groups"
MAX_APP_GROUPS = 50

LIMITS = (
    "Composite roles are not expanded and a user's direct role mappings are not read; an app that "
    "decides by a group claim only appears where the admin said which groups it reads."
)


def utcnow_ts() -> float:
    return time.time()


def _s(value: Any, limit: int = 200) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _rows(raw: dict[str, Any], name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The rows of one section of the node's answer, and what to say about the section: a missing
    or failed section is an empty list **with** a note, never silently empty."""
    sec = raw.get(name)
    if not isinstance(sec, dict) or sec.get("ok") is not True:
        err = (sec or {}).get("error") if isinstance(sec, dict) else None
        return [], {"ok": False, "error": _s(err, 300) or "not answered by the node"}
    data = [r for r in sec.get("data") or [] if isinstance(r, dict)]
    return data, {
        "ok": True,
        "total": sec.get("total", len(data)),
        "truncated": bool(sec.get("truncated")),
    }


def _string_list(value: Any, limit: int = 100) -> list[str]:
    return [v[:200] for v in value if isinstance(v, str)][:limit] if isinstance(value, list) else []


def ancestors(path: str) -> list[str]:
    """``/a/b/c`` -> ``["/a", "/a/b"]`` (the groups whose roles a member of ``/a/b/c`` inherits)."""
    parts = [p for p in path.split("/") if p]
    return ["/" + "/".join(parts[:i]) for i in range(1, len(parts))]


def build(raw: dict[str, Any], app_groups: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """The people, groups and apps of one ``keycloak.snapshot`` answer with the chain between
    them. A pure function: the tests feed it canned answers."""
    app_groups = app_groups or {}
    users, users_state = _rows(raw, "users")
    groups, groups_state = _rows(raw, "groups")
    apps, apps_state = _rows(raw, "apps")
    notes: list[str] = [LIMITS]
    for label, state in (("users", users_state), ("groups", groups_state), ("apps", apps_state)):
        if not state["ok"]:
            notes.append(
                f"the {label} could not be read ({state['error']}): what follows is incomplete"
            )
        elif state["truncated"]:
            notes.append(f"the {label} list was cut at {state['total']}: more exist")

    by_group: dict[str, dict[str, Any]] = {}
    for g in groups:
        path = _s(g.get("path"))
        if not path:
            continue
        by_group[path] = {
            "id": _s(g.get("id")),
            "path": path,
            "name": _s(g.get("name")) or path.rsplit("/", 1)[-1],
            "members": _string_list(g.get("members"), 1000),
            "members_ok": g.get("members_ok") is not False,
            "client_roles": {
                _s(app): _string_list(roles)
                for app, roles in (g.get("client_roles") or {}).items()
                if isinstance(roles, list) and _s(app)
            }
            if isinstance(g.get("client_roles"), dict)
            else {},
            "realm_roles": _string_list(g.get("realm_roles")),
        }
        if not by_group[path]["members_ok"]:
            notes.append(f"the members of {path} could not be read: its people are missing")

    app_rows: dict[str, dict[str, Any]] = {}
    for a in apps:
        cid = _s(a.get("client_id"))
        if cid:
            app_rows[cid] = {
                "client_id": cid,
                "name": _s(a.get("name")) or cid,
                "enabled": a.get("enabled") is not False,
                "builtin": a.get("builtin") is True,
                "url": _s(a.get("url"), 300),
                "roles": _string_list(a.get("roles")),
            }

    # what each group opens: its own client-role mappings and those of its parents, and the apps
    # the admin said read it (marked by hand)
    def opens(path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for source in [*ancestors(path), path]:
            g = by_group.get(source)
            if g:
                for app, roles in g["client_roles"].items():
                    out.append({"app": app, "roles": roles, "via_group": source, "kind": "role"})
            for app, wanted in app_groups.items():
                if source in wanted:
                    out.append({"app": app, "roles": [], "via_group": source, "kind": "configured"})
        return out

    member_of: dict[str, list[str]] = {}
    for path, g in by_group.items():
        for uid in g["members"]:
            member_of.setdefault(uid, []).append(path)

    people: list[dict[str, Any]] = []
    for u in users:
        uid = _s(u.get("id"))
        if not uid:
            continue
        paths = sorted(member_of.get(uid, []))
        access: dict[str, dict[str, Any]] = {}
        for p in paths:
            for hit in opens(p):
                entry = access.setdefault(hit["app"], {"app": hit["app"], "roles": [], "via": []})
                entry["roles"] = sorted({*entry["roles"], *hit["roles"]})
                via = {"group": hit["via_group"], "member_of": p, "kind": hit["kind"]}
                if via not in entry["via"]:
                    entry["via"].append(via)
        for entry in access.values():
            row = app_rows.get(entry["app"])
            entry["name"] = row["name"] if row else entry["app"]
            entry["known_app"] = row is not None
        people.append(
            {
                "id": uid,
                "username": _s(u.get("username")),
                "name": _s(u.get("name")),
                "enabled": u.get("enabled") is not False,
                "source": u.get("source")
                if u.get("source") in ("directory", "local")
                else "unknown",
                "groups": paths,
                "apps": sorted(access.values(), key=lambda e: e["app"]),
            }
        )
    people.sort(key=lambda p: (p["username"].lower(), p["id"]))

    group_rows: list[dict[str, Any]] = []
    for path in sorted(by_group):
        g = by_group[path]
        group_rows.append(
            {
                "id": g["id"],
                "path": path,
                "name": g["name"],
                "members": len(g["members"]),
                "members_ok": g["members_ok"],
                "realm_roles": g["realm_roles"],
                "opens": opens(path),
            }
        )

    app_out: list[dict[str, Any]] = []
    for cid in sorted({*app_rows, *{a["app"] for p in people for a in p["apps"]}}):
        row = app_rows.get(cid) or {
            "client_id": cid,
            "name": cid,
            "enabled": True,
            "builtin": False,
            "url": "",
            "roles": [],
        }
        who = [p for p in people if any(a["app"] == cid for a in p["apps"])]
        via = sorted(
            {v["group"] for p in who for a in p["apps"] if a["app"] == cid for v in a["via"]}
        )
        app_out.append(
            {
                **row,
                "known_app": cid in app_rows,
                "people": len(who),
                "people_enabled": len([p for p in who if p["enabled"]]),
                "groups": via,
                "configured_groups": app_groups.get(cid, []),
            }
        )
    return {
        "realm": _s(raw.get("realm"), 100),
        "node_taken_at": _s(raw.get("taken_at"), 40) or None,
        "people": people,
        "groups": group_rows,
        "apps": app_out,
        "notes": notes,
    }


def explain(model: dict[str, Any], person: str, app: str) -> dict[str, Any]:
    """Why can this person (username or name) use this app — or why not, as far as the look
    shows. Names are matched exactly (case-insensitive) first, then by "contains" when that is
    unambiguous."""

    def pick(rows: list[dict[str, Any]], key: tuple[str, ...], text: str) -> list[dict[str, Any]]:
        t = text.strip().lower()
        exact = [r for r in rows if any(str(r.get(k, "")).lower() == t for k in key)]
        if exact:
            return exact
        return [r for r in rows if any(t in str(r.get(k, "")).lower() for k in key)]

    people = pick(model["people"], ("username", "name"), person)
    apps = pick(model["apps"], ("client_id", "name"), app)
    if len(people) != 1 or len(apps) != 1:
        return {
            "found": False,
            "people_matching": [p["username"] for p in people][:10],
            "apps_matching": [a["client_id"] for a in apps][:10],
        }
    p, a = people[0], apps[0]
    hit = next((x for x in p["apps"] if x["app"] == a["client_id"]), None)
    out: dict[str, Any] = {
        "found": True,
        "person": p["username"],
        "app": a["client_id"],
        "enabled_user": p["enabled"],
        "enabled_app": a["enabled"],
        "has_access": hit is not None,
        "member_of": p["groups"],
    }
    if hit:
        out["roles"] = hit["roles"]
        out["via"] = hit["via"]
    else:
        out["why_not"] = (
            "no group of this person carries a role of this app"
            + (" and none is listed as read by it" if a["configured_groups"] else "")
            + ". If the app decides in another way (a direct user mapping, a composite role, its "
            "own list), this look cannot show it."
        )
        out["groups_that_would_open_it"] = a["groups"] + a["configured_groups"]
    return out


class IdentityService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        tools: ToolService,
        settings: Settings,
    ) -> None:
        self.sm = sessionmaker
        self.tools = tools
        self.settings = settings
        # per node: (read at, answer). Memory only: it is not stored, so no row can outlive the
        # right to see it; every call re-checks that the caller may run the capability.
        self._cache: dict[uuid.UUID, tuple[float, dict[str, Any]]] = {}

    # --- who may look -----------------------------------------------------------------------

    async def sources(self, caller: Caller) -> list[dict[str, Any]]:
        """The connected nodes whose ``keycloak.snapshot`` this caller may run."""
        return [t for t in await self.tools.usable_tools(caller) if t["capability"] == CAPABILITY]

    async def offer(self, caller: Caller) -> list[dict[str, Any]]:
        """The chat's virtual tool, if the caller may look at some node's identities."""
        return [query_tool()] if await self.sources(caller) else []

    # --- settings -----------------------------------------------------------------------------

    async def app_groups(self) -> dict[str, list[str]]:
        async with self.sm() as db:
            value = await get_hub_setting(db, APP_GROUPS_KEY)
        return clean_app_groups(value)

    async def set_app_groups(self, value: Any) -> dict[str, list[str]]:
        cleaned = clean_app_groups(value)
        async with self.sm() as db:
            await set_hub_setting(db, APP_GROUPS_KEY, cleaned or None)
            await db.commit()
        return cleaned

    # --- reading ----------------------------------------------------------------------------

    async def read(self, caller: Caller, node_id: uuid.UUID, refresh: bool) -> dict[str, Any]:
        """One node's snapshot as the person may see it, from the cache when it is young."""
        ttl = self.settings.identity_cache_seconds
        hit = self._cache.get(node_id)
        now = utcnow_ts()
        if not refresh and hit is not None and now - hit[0] < ttl:
            return {"read_at": hit[0], "raw": hit[1], "cached": True}
        run = await self.tools.invoke(caller, node_id, CAPABILITY, {})
        if not run.get("ok"):
            err = run.get("error") or {}
            raise ToolError(
                502, err.get("code", "error"), str(err.get("message") or "the node failed")
            )
        raw = run.get("output")
        if not isinstance(raw, dict):
            raise ToolError(502, "bad_answer", "the node's answer is not understood")
        self._cache[node_id] = (now, raw)
        return {"read_at": now, "raw": raw, "cached": False}

    async def view(self, caller: Caller, refresh: bool = False) -> dict[str, Any]:
        """Everything the caller may see: one entry per source node, each with its own look."""
        sources = await self.sources(caller)
        groups = await self.app_groups()
        out: list[dict[str, Any]] = []
        for src in sorted(sources, key=lambda s: s["node"]):
            entry: dict[str, Any] = {"node": {"id": src["node_id"], "name": src["node"]}}
            try:
                got = await self.read(caller, uuid.UUID(src["node_id"]), refresh)
            except ToolError as exc:
                entry["error"] = {"code": exc.code, "message": exc.message}
            else:
                entry.update(build(got["raw"], groups))
                entry["read_at"] = got["read_at"]
                entry["age_seconds"] = max(0, int(utcnow_ts() - got["read_at"]))
                entry["cached"] = got["cached"]
            out.append(entry)
        return {"sources": out, "app_groups": groups}

    # --- the chat's tool ------------------------------------------------------------------------

    async def run_tool(self, caller: Caller, tool_input: dict[str, Any]) -> dict[str, Any]:
        unknown = set(tool_input) - set(QUERY_SCHEMA["properties"])
        if unknown:
            raise ToolError(422, "invalid_input", f"unknown input: {', '.join(sorted(unknown))}")
        for key, val in tool_input.items():
            if not isinstance(val, str) or len(val) > 100:
                raise ToolError(422, "invalid_input", f"{key} must be a short text")
        person, app, group, query = (
            tool_input.get(k, "") for k in ("person", "app", "group", "query")
        )
        if not (person or app or group or query):
            raise ToolError(422, "invalid_input", "ask about a person, an app, a group or a text")
        views = (await self.view(caller))["sources"]
        results: list[dict[str, Any]] = []
        notes: list[str] = []
        for v in views:
            if "error" in v:
                notes.append(f"{v['node']['name']}: could not be read ({v['error']['message']})")
                continue
            notes.extend(f"{v['node']['name']}: {n}" for n in v["notes"])
            if v["age_seconds"] > 120:
                notes.append(f"{v['node']['name']}: this look is {v['age_seconds']} s old")
            if person and app:
                results.append({"node": v["node"]["name"], **explain(v, person, app)})
            elif person:
                results.extend(
                    {"node": v["node"]["name"], "kind": "person", **_person_brief(p)}
                    for p in v["people"]
                    if _match(p, person, ("username", "name"))
                )
            elif app:
                results.extend(
                    {"node": v["node"]["name"], "kind": "app", **_app_brief(a)}
                    for a in v["apps"]
                    if _match(a, app, ("client_id", "name"))
                )
            elif group:
                results.extend(
                    {"node": v["node"]["name"], "kind": "group", **g}
                    for g in v["groups"]
                    if _match(g, group, ("path", "name"))
                )
            else:
                results.extend(_search(v, query))
        out = {"results": results[:30], "count": len(results), "truncated": len(results) > 30}
        if notes:
            out["caveats"] = sorted(set(notes))
        if not views:
            out["caveats"] = ["no node offers identities to you"]
        return {"ok": True, "output": out, "at": None, "bytes": len(str(out))}


def _match(row: dict[str, Any], text: str, keys: tuple[str, ...]) -> bool:
    t = text.strip().lower()
    return any(t in str(row.get(k, "")).lower() for k in keys)


def _person_brief(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": p["username"],
        "name": p["name"],
        "enabled": p["enabled"],
        "source": p["source"],
        "groups": p["groups"],
        "apps": [{"app": a["app"], "roles": a["roles"], "via": a["via"]} for a in p["apps"]],
    }


def _app_brief(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "app": a["client_id"],
        "name": a["name"],
        "enabled": a["enabled"],
        "roles": a["roles"],
        "people": a["people"],
        "groups": a["groups"],
        "configured_groups": a["configured_groups"],
    }


def _search(view: dict[str, Any], query: str) -> list[dict[str, Any]]:
    node = view["node"]["name"]
    out: list[dict[str, Any]] = []
    out += [
        {"node": node, "kind": "person", **_person_brief(p)}
        for p in view["people"]
        if _match(p, query, ("username", "name"))
    ]
    out += [
        {"node": node, "kind": "group", **g}
        for g in view["groups"]
        if _match(g, query, ("path", "name"))
    ]
    out += [
        {"node": node, "kind": "app", **_app_brief(a)}
        for a in view["apps"]
        if _match(a, query, ("client_id", "name"))
    ]
    return out


def clean_app_groups(value: Any) -> dict[str, list[str]]:
    """``{"homarr": ["/family", "/admins"]}``: an app (its client id) and the Keycloak group paths
    it reads from the token by itself. Anything else is dropped."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for app, paths in list(value.items())[:MAX_APP_GROUPS]:
        if not isinstance(app, str) or not app.strip() or not isinstance(paths, list):
            continue
        clean = sorted(
            {p.strip()[:200] for p in paths if isinstance(p, str) and p.strip().startswith("/")}
        )
        if clean:
            out[app.strip()[:200]] = clean[:MAX_APP_GROUPS]
    return out


QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "person": {
            "type": "string",
            "maxLength": 100,
            "description": "a user name or display name",
        },
        "app": {
            "type": "string",
            "maxLength": 100,
            "description": "an app (Keycloak client id or name)",
        },
        "group": {"type": "string", "maxLength": 100, "description": "a group name or path"},
        "query": {
            "type": "string",
            "maxLength": 100,
            "description": "free text over people, groups and apps",
        },
    },
}
QUERY_DESCRIPTION = (
    "The identities of the user's lab (read from Keycloak through a tool node): which person is in "
    "which group, which group opens which app with which role, and who can therefore use an app. "
    "Ask with 'person' for one person's groups and apps, with 'app' for who may use it and through "
    "which groups, with 'group' for a group's members count and what it opens, with 'person' AND "
    "'app' for the chain that explains why somebody can or cannot use an app. It is a stored look "
    "(its age is in the answer), it shows role mappings only — composite roles and apps that read "
    "groups from the token by themselves are not visible unless the answer says so — so never say "
    "somebody 'has no access' without naming that limit. It changes nothing."
)


def query_tool() -> dict[str, Any]:
    return {
        "node_id": str(IDENTITY_NODE_ID),
        "node": IDENTITY_NODE,
        "capability": QUERY_CAPABILITY,
        "description": QUERY_DESCRIPTION,
        "input_schema": QUERY_SCHEMA,
    }
