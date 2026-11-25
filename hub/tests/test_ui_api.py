"""The interface as data over the API (D60, D70): what a person is offered (the programs), their
choices (the desktop), the admin's programs and profiles, isolation between people (D20), and the
events that tell open pages."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.users import make_user


def boot(client: TestClient, headers: dict[str, str] = AUTH) -> dict[str, Any]:
    r = client.get("/api/ui/bootstrap", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def by_key(boot_doc: dict[str, Any]) -> dict[str, Any]:
    return {w["key"]: w for w in boot_doc["workspaces"]}


@pytest.fixture
def user_b(client: TestClient, clean_db: str) -> dict[str, Any]:
    uid, headers = make_user(clean_db, "user-b")
    return {"id": str(uid), "h": headers}


def set_profile(client: TestClient, user_id: str, profile: str | None) -> None:
    r = client.put(f"/api/ui/users/{user_id}/profile", headers=AUTH, json={"profile": profile})
    assert r.status_code == 200, r.text


# --- what a person is offered --------------------------------------------------------------------


def test_the_admin_is_offered_everything_the_hub_ships(client: TestClient) -> None:
    b = boot(client)
    assert b["v"] == 1
    assert b["access"] == {"admin": True, "profile": None, "denied_tools": []}
    # the programs of the desktop, by position
    assert list(by_key(b)) == [
        "chat",
        "research",
        "project",
        "wiki",
        "sheets",
        "dev",
        "files",
        "incognito",
        "admin",
        "settings",
    ]
    assert b["prefs"]["hue"] == 140 and b["prefs"]["desktop"]["open"] == ["chat"]
    assert "limits" not in b
    modules = {m["id"]: m for m in b["modules"]}
    assert modules["page.chat"]["allowed"] and modules["page.access"]["allowed"]
    chat = by_key(b)["chat"]
    # Start (key chat, 2026-11-13): the overview of the applications first, then the inbox
    assert chat["modules"][:3] == ["page.home", "page.inbox", "page.chat"]
    assert chat["open"] == ["page.home"] and chat["title"] == "Start"
    assert "layouts" not in chat and "locked" not in chat and chat["builtin"] is True


def test_a_person_without_a_profile_gets_the_default_one(client: TestClient, user_b: dict) -> None:
    b = boot(client, user_b["h"])
    assert b["access"]["profile"] == "default" and b["access"]["admin"] is False
    # the admin programs are not even listed; Inbox, Incognito and Settings are everybody's
    assert list(by_key(b)) == ["chat", "research", "incognito", "settings"]
    modules = {m["id"]: m for m in b["modules"]}
    assert modules["page.chat"]["allowed"] is True
    assert modules["page.overview"]["allowed"] is False  # an admin page
    assert modules["page.knowledge"]["allowed"] is True  # needs "notes": not denied


def test_a_workspace_that_is_not_offered_does_not_exist_for_the_person(
    client: TestClient, user_b: dict
) -> None:
    assert client.get("/api/ui/workspaces/admin/agent", headers=user_b["h"]).status_code == 404
    assert client.get("/api/ui/workspaces/nonsense/agent", headers=user_b["h"]).status_code == 404
    # the layout routes of the old shells are gone
    assert client.delete("/api/ui/layouts/chat", headers=AUTH).status_code == 404


def test_the_calls_need_a_token(client: TestClient) -> None:
    assert client.get("/api/ui/bootstrap").status_code == 401
    assert client.put("/api/ui/prefs", json={}).status_code == 401
    assert client.get("/api/ui/templates").status_code == 401


# --- the presets of the admin --------------------------------------------------------------------


# --- the choices of a person ---------------------------------------------------------------------


def test_prefs_are_checked_and_replaced(client: TestClient) -> None:
    r = client.put("/api/ui/prefs", headers=AUTH, json={"hue": 30})
    assert r.status_code == 200 and r.json()["hue"] == 30
    desktop = {
        "open": ["chat", "admin", "chat"],
        "minimised": ["admin"],
        "focus": "chat",
        "maximised": None,
        "views": {"chat": {"module": "page.chat", "props": {"id": "c1"}}},
    }
    r = client.put("/api/ui/prefs", headers=AUTH, json={"desktop": desktop})
    assert r.status_code == 200
    d = r.json()["desktop"]
    assert d["open"] == ["chat", "admin"] and d["minimised"] == ["admin"]
    assert r.json()["hue"] == 30  # one change, one field
    assert client.get("/api/ui/prefs", headers=AUTH).json() == r.json()
    assert client.put("/api/ui/prefs", headers=AUTH, json={"hue": 400}).status_code == 422
    assert client.put("/api/ui/prefs", headers=AUTH, json={"shell": "desk"}).status_code == 422
    assert client.put("/api/ui/prefs", headers=AUTH, json={"theme": {"hue": 1}}).status_code == 422
    assert client.put("/api/ui/prefs", headers=AUTH, json={"desktop": []}).status_code == 422
    # a refused change changed nothing
    assert client.get("/api/ui/prefs", headers=AUTH).json() == r.json()
    assert client.get("/api/ui/prefs/defaults", headers=AUTH).json()["prefs"]["hue"] == 140
    r = client.put("/api/ui/prefs", headers=AUTH, json={"look": "leitstand"})
    assert r.status_code == 200 and r.json()["look"] == "leitstand"
    assert client.put("/api/ui/prefs", headers=AUTH, json={"look": "Neon Sign"}).status_code == 422


def test_prefs_belong_to_one_person(client: TestClient, user_b: dict) -> None:
    client.put("/api/ui/prefs", headers=AUTH, json={"hue": 10})
    assert client.get("/api/ui/prefs", headers=user_b["h"]).json()["hue"] == 140
    client.put("/api/ui/prefs", headers=user_b["h"], json={"hue": 200})
    assert client.get("/api/ui/prefs", headers=AUTH).json()["hue"] == 10


# --- isolation (D20) -----------------------------------------------------------------------------


def test_the_admin_api_is_for_the_admin_and_not_for_a_key(client: TestClient, user_b: dict) -> None:
    h = user_b["h"]
    for method, path in (
        ("GET", "/api/ui/templates"),
        ("GET", "/api/ui/templates/chat"),
        ("PUT", "/api/ui/templates/x"),
        ("POST", "/api/ui/templates/chat/duplicate"),
        ("DELETE", "/api/ui/templates/chat"),
        ("GET", "/api/ui/profiles"),
        ("PUT", "/api/ui/profiles/default"),
        ("DELETE", "/api/ui/profiles/guest"),
        ("GET", "/api/ui/users"),
        ("PUT", f"/api/ui/users/{user_b['id']}/profile"),
    ):
        r = client.request(method, path, headers=h, json={})
        assert r.status_code == 403, (method, path, r.status_code)


# --- the agent of a workspace --------------------------------------------------------------------


def test_the_agent_defaults_of_a_workspace_are_narrowed_by_the_profile(
    client: TestClient, user_b: dict
) -> None:
    r = client.get("/api/ui/workspaces/dev/agent", headers=AUTH)
    assert r.status_code == 200
    a = r.json()
    assert a["workspace"] == "dev"
    # Development: the workshop, GitHub, the notes and the wiki (read only), and no web
    assert a["tool_scope"] == {
        "nodes": None,
        "groups": ["notes", "github", "workshop", "trace", "wiki"],
        "internet": False,
    }
    assert a["denied"] == [] and a["planned"] == []
    # the research workspace starts with the notes and the wiki (2026-11-18)
    k = client.get("/api/ui/workspaces/research/agent", headers=user_b["h"]).json()
    assert k["tool_scope"] == {"nodes": None, "groups": ["notes", "wiki"], "internet": True}
    # a profile that denies the notes takes them away, and the web with "web"
    client.put(
        "/api/ui/profiles/default",
        headers=AUTH,
        json={
            "default": True,
            "grants": [
                {"kind": "workspace", "key": "research", "effect": "allow"},
                {"kind": "workspace", "key": "chat", "effect": "allow"},
                {"kind": "tool", "key": "notes", "effect": "deny"},
                {"kind": "tool", "key": "web", "effect": "deny"},
            ],
        },
    )
    k = client.get("/api/ui/workspaces/research/agent", headers=user_b["h"]).json()
    assert k["tool_scope"]["groups"] == ["wiki"] and k["tool_scope"]["internet"] is False
    assert k["denied"] == ["notes"]
    # "chat" has the notes, the wiki and the web; the profile takes the notes and the web away
    t = client.get("/api/ui/workspaces/chat/agent", headers=user_b["h"]).json()
    assert t["tool_scope"]["internet"] is False and t["tool_scope"]["groups"] == ["wiki"]
    assert t["denied"] == ["notes"]
    # the admin is never narrowed by a profile, only by the application itself
    admin = client.get("/api/ui/workspaces/chat/agent", headers=AUTH).json()["tool_scope"]
    assert admin == {"nodes": None, "groups": ["notes", "wiki"], "internet": True}


def test_planned_groups_are_kept_and_reported(client: TestClient) -> None:
    body = {
        "title": "Office",
        "modules": ["page.chat"],
        "agent": {
            "tool_scope": {"groups": ["docs", "mail", "notes", "bogus"], "internet": True},
            "persona": "sachlich",
            "model_role": "deep",
        },
    }
    assert client.put("/api/ui/templates/office", headers=AUTH, json=body).status_code == 201
    t = client.get("/api/ui/templates/office", headers=AUTH).json()
    assert t["agent"]["tool_scope"]["groups"] == [
        "notes",
        "docs",
        "mail",
    ]  # bogus dropped, order fixed
    a = client.get("/api/ui/workspaces/office/agent", headers=AUTH).json()
    assert a["tool_scope"] == {"nodes": None, "groups": ["notes"], "internet": True}
    assert a["planned"] == ["docs", "mail"] and a["model_role"] == "deep"


# --- the admin: workspaces -----------------------------------------------------------------------


def test_templates_are_created_changed_duplicated_and_deleted(client: TestClient) -> None:
    body = {
        "title": "My room",
        "icon": "@",
        "modules": ["page.chat", "page.memory"],
        "position": 50,
    }
    r = client.put("/api/ui/templates/my-room", headers=AUTH, json=body)
    assert r.status_code == 201 and r.json()["rev"] == 1 and r.json()["builtin"] is False
    r = client.put(
        "/api/ui/templates/my-room", headers=AUTH, json={**body, "title": "My other room"}
    )
    assert r.status_code == 200 and r.json()["rev"] == 2 and r.json()["title"] == "My other room"
    keys = [t["key"] for t in client.get("/api/ui/templates", headers=AUTH).json()["templates"]]
    # position 50: between Development (40) and Incognito (60)
    assert keys == [
        "chat",
        "research",
        "project",
        "wiki",
        "sheets",
        "dev",
        "files",
        "my-room",
        "incognito",
        "admin",
        "settings",
    ]
    r = client.post("/api/ui/templates/dev/duplicate", headers=AUTH, json={"key": "dev-2"})
    assert (
        r.status_code == 201
        and r.json()["builtin"] is False
        and r.json()["title"] == "Development (copy)"
    )
    # the application's own pages come first; its chat is the Chat application's, not its window
    assert r.json()["modules"][:2] == ["page.workshop", "page.repos"]
    assert "page.workshop" in r.json()["modules"] and "page.chat" not in r.json()["modules"]
    assert (
        client.post(
            "/api/ui/templates/dev/duplicate", headers=AUTH, json={"key": "dev-2"}
        ).status_code
        == 409
    )
    assert (
        client.post("/api/ui/templates/nope/duplicate", headers=AUTH, json={"key": "z"}).status_code
        == 404
    )
    assert client.delete("/api/ui/templates/my-room", headers=AUTH).json() == {"deleted": "my-room"}
    assert client.get("/api/ui/templates/my-room", headers=AUTH).status_code == 404
    assert client.delete("/api/ui/templates/my-room", headers=AUTH).status_code == 404


def test_a_built_in_workspace_cannot_be_changed_or_deleted(client: TestClient) -> None:
    body = {"title": "Chat!", "modules": ["page.chat"]}
    r = client.put("/api/ui/templates/chat", headers=AUTH, json=body)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "builtin"
    assert client.delete("/api/ui/templates/chat", headers=AUTH).status_code == 409
    assert client.get("/api/ui/templates/chat", headers=AUTH).json()["title"] == "Start"


@pytest.mark.parametrize(
    ("key", "body", "code"),
    [
        ("Bad Key", {"title": "x", "modules": []}, "key"),
        ("ok", {"title": " ", "modules": []}, "title"),
        ("ok", {"title": "x", "modules": ["page.nope"]}, "modules"),
        ("ok", {"title": "x", "modules": ["page.chat", "page.chat"]}, "modules"),
        ("ok", {"title": "x", "modules": [f"m{i}" for i in range(25)]}, "modules"),
        ("ok", {"title": "x", "modules": [], "requires_role": "root"}, "requires_role"),
        ("ok", {"title": "x", "modules": [], "position": 5000}, "position"),
        ("ok", {"title": "x", "modules": [], "icon": "123456789"}, "icon"),
    ],
)
def test_a_template_that_makes_no_sense_is_refused(
    client: TestClient, key: str, body: dict, code: str
) -> None:
    r = client.put(f"/api/ui/templates/{key}", headers=AUTH, json=body)
    assert r.status_code in (404, 422)
    if r.status_code == 422 and isinstance(r.json()["detail"], dict):
        assert r.json()["detail"]["code"] == code
    assert len(client.get("/api/ui/templates", headers=AUTH).json()["templates"]) == 10


# --- the admin: profiles and people --------------------------------------------------------------


def test_profiles_are_listed_changed_and_deleted(client: TestClient, user_b: dict) -> None:
    profiles = {
        p["name"]: p for p in client.get("/api/ui/profiles", headers=AUTH).json()["profiles"]
    }
    assert set(profiles) == {"default", "guest"}
    assert profiles["default"]["default"] is True and "locked" not in profiles["guest"]
    assert {"kind": "workspace", "key": "chat", "effect": "allow"} in profiles["guest"]["grants"]
    body = {
        "description": "The family",
        "grants": [
            {"kind": "workspace", "key": "chat", "effect": "allow"},
            {"kind": "workspace", "key": "research", "effect": "allow"},
            {"kind": "tool", "key": "shell", "effect": "deny"},
            {"kind": "tool", "key": "shell", "effect": "allow"},  # the last line for a key wins
        ],
    }
    r = client.put("/api/ui/profiles/family", headers=AUTH, json=body)
    assert r.status_code == 200 and r.json()["default"] is False and r.json()["builtin"] is False
    assert {"kind": "tool", "key": "shell", "effect": "allow"} in r.json()["grants"]
    set_profile(client, user_b["id"], "family")
    users = {u["name"]: u for u in client.get("/api/ui/users", headers=AUTH).json()["users"]}
    assert users["user-b"]["profile"] == "family"
    assert not any(
        u["name"] in ("household", "system-rules") for u in users.values()
    )  # pseudo users
    assert boot(client, user_b["h"])["access"]["profile"] == "family"
    # deleting a profile puts its people back on the default one
    assert client.delete("/api/ui/profiles/family", headers=AUTH).json() == {"deleted": "family"}
    assert boot(client, user_b["h"])["access"]["profile"] == "default"
    assert client.delete("/api/ui/profiles/guest", headers=AUTH).status_code == 409  # built in
    assert client.delete("/api/ui/profiles/default", headers=AUTH).status_code == 409
    assert client.delete("/api/ui/profiles/nope", headers=AUTH).status_code == 404


def test_the_default_profile_can_be_moved(client: TestClient, user_b: dict) -> None:
    client.put(
        "/api/ui/profiles/kids",
        headers=AUTH,
        json={"default": True, "grants": [{"kind": "workspace", "key": "chat", "effect": "allow"}]},
    )
    profiles = {
        p["name"]: p for p in client.get("/api/ui/profiles", headers=AUTH).json()["profiles"]
    }
    assert [n for n, p in profiles.items() if p["default"]] == ["kids"]
    # the programs everybody has stay; of the others only what the new default allows
    assert list(by_key(boot(client, user_b["h"]))) == ["chat", "incognito", "settings"]


@pytest.mark.parametrize(
    "grants",
    [
        [{"kind": "page", "key": "chat", "effect": "allow"}],
        [{"kind": "tool", "key": "shell", "effect": "maybe"}],
        [{"kind": "tool", "key": "Bad Key", "effect": "deny"}],
    ],
)
def test_a_grant_that_makes_no_sense_is_refused(client: TestClient, grants: list) -> None:
    r = client.put("/api/ui/profiles/x", headers=AUTH, json={"grants": grants})
    assert r.status_code == 422
    assert {p["name"] for p in client.get("/api/ui/profiles", headers=AUTH).json()["profiles"]} == {
        "default",
        "guest",
    }


def test_a_person_and_a_profile_must_exist(client: TestClient, user_b: dict) -> None:
    assert (
        client.put(
            f"/api/ui/users/{user_b['id']}/profile", headers=AUTH, json={"profile": "nope"}
        ).status_code
        == 404
    )
    unknown = "11111111-1111-4111-8111-111111111111"
    assert (
        client.put(
            f"/api/ui/users/{unknown}/profile", headers=AUTH, json={"profile": None}
        ).status_code
        == 404
    )
    set_profile(client, user_b["id"], "guest")
    set_profile(client, user_b["id"], None)  # back to the default
    assert boot(client, user_b["h"])["access"]["profile"] == "default"


# --- the events ----------------------------------------------------------------------------------


def drain(queue) -> list[tuple[str, dict[str, Any]]]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_changes_are_announced_on_the_stream(client: TestClient, user_b: dict) -> None:
    from topcoat_hub.models import DEV_ADMIN_ID

    queue = client.app.state.bus.subscribe()
    try:
        client.put("/api/ui/prefs", headers=AUTH, json={"hue": 20})
        client.put(
            "/api/ui/templates/room", headers=AUTH, json={"title": "Room", "modules": ["page.chat"]}
        )
        events = drain(queue)
    finally:
        client.app.state.bus.unsubscribe(queue)
    assert [e for e, _ in events] == ["ui_prefs", "ui_access"]
    # a person's events carry the person (the stream hands them to that person only, and strips it)
    assert events[0][1] == {"_user": str(DEV_ADMIN_ID)}
    assert events[1][1] == {}  # everybody refetches what they are offered


def test_a_refused_change_is_not_announced(client: TestClient) -> None:
    queue = client.app.state.bus.subscribe()
    try:
        client.put("/api/ui/prefs", headers=AUTH, json={"hue": 999})
        client.put("/api/ui/templates/chat", headers=AUTH, json={"title": "x", "modules": []})
        assert drain(queue) == []
    finally:
        client.app.state.bus.unsubscribe(queue)
