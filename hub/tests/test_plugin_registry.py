"""The registry of data plug-ins (D105 step 2): built-in programs registered, install, update,
roll back, disable and remove while the hub runs, the rule that a plug-in only narrows rights,
the audit and the live event."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub import app_scope, plugin_registry
from topcoat_hub.auth import CurrentUser
from topcoat_hub.models import DEV_ADMIN_ID


def manifest(key: str = "notes-board", version: str = "1.0.0", **extra: Any) -> dict[str, Any]:
    return {
        "id": key,
        "version": version,
        "title_key": f"plugins.{key}",
        "views": [{"id": "board", "type": "board", "source": "notes"}],
        "commands": [
            {
                "id": f"view.{key}",
                "label_key": f"plugins.{key}.open",
                "action": {"kind": "open_view", "view": "board"},
            }
        ],
        **extra,
    }


def command_ids(client: TestClient, headers: dict[str, str] = AUTH) -> set[str]:
    r = client.get("/api/ui/commands", headers=headers)
    assert r.status_code == 200, r.text
    return {c["id"] for c in r.json()["commands"]}


def install(client: TestClient, m: dict[str, Any]) -> dict[str, Any]:
    r = client.post("/api/plugins", headers=AUTH, json=m)
    assert r.status_code == 201, r.text
    return r.json()


def drain(client: TestClient, queue: Any) -> list[tuple[str, dict[str, Any]]]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return [e for e in out if e[0] == plugin_registry.EVENT]


def test_the_built_in_programs_are_registered_as_version_one(client: TestClient) -> None:
    rows = {p["key"]: p for p in client.get("/api/plugins", headers=AUTH).json()["plugins"]}
    assert {"chat", "research", "dev", "project", "admin", "settings"} <= set(rows)
    for key in ("chat", "research", "dev", "admin"):
        assert rows[key]["version"] == "1.0.0" and rows[key]["builtin"] and rows[key]["enabled"]
    detail = client.get("/api/plugins/research", headers=AUTH).json()
    [v] = detail["versions"]
    assert v["manifest"]["builtin"] is True
    # the manifest states the program's ceiling as it is: registering changes nothing
    assert sorted(v["manifest"]["agent"]["tools_ceiling"]) == ["internet", "notes", "wiki"]
    assert detail["changes"][0]["action"] == "register"
    # a second start writes nothing new
    assert client.portal.call(plugin_registry.ensure_builtin, client.app.state.sessionmaker) == 0


def test_the_admin_api_is_for_the_admin_in_the_gui_only(client: TestClient) -> None:
    _, user = make_user(TEST_DB_URL, "plain", role="user")
    _, admin_key = make_user(TEST_DB_URL, "scripted-admin", role="admin")
    for headers in (user, admin_key):  # an API key never opens it, not even an admin's
        assert client.get("/api/plugins", headers=headers).status_code == 403
        assert client.post("/api/plugins", headers=headers, json=manifest()).status_code == 403
        assert client.delete("/api/plugins/chat", headers=headers).status_code == 403


def test_a_plugin_is_installed_updated_rolled_back_and_removed_while_the_hub_runs(
    client: TestClient,
) -> None:
    queue = client.app.state.bus.subscribe()
    install(client, manifest())
    assert "view.notes-board" in command_ids(client)
    assert drain(client, queue) == [("ui_plugins", {"key": "notes-board"})]

    # an update is a new version with a higher number; the old one is kept
    v2 = manifest(version="1.1.0")
    v2["commands"][0]["shortcut"] = "Ctrl+B"
    v2["commands"].append(
        {"id": "view.notes-board.new", "label_key": "x", "action": {"kind": "open_view"}}
    )
    r = client.post("/api/plugins/notes-board/versions", headers=AUTH, json=v2)
    assert r.status_code == 201, r.text
    assert r.json()["version"] == "1.1.0"
    assert "view.notes-board.new" in command_ids(client)
    assert drain(client, queue) == [("ui_plugins", {"key": "notes-board"})]
    for stale in ("1.1.0", "1.0.5"):
        r = client.post(
            "/api/plugins/notes-board/versions", headers=AUTH, json=manifest(version=stale)
        )
        assert r.status_code == 409 and r.json()["detail"]["code"] == "version_exists"

    d = client.get("/api/plugins/notes-board/diff?a=1.0.0&b=1.1.0", headers=AUTH).json()
    assert any(line.startswith("+") and "Ctrl+B" in line for line in d["lines"])

    # roll back: the earlier version is in force again, at once
    r = client.post("/api/plugins/notes-board/rollback", headers=AUTH, json={"version": "1.0.0"})
    assert r.status_code == 200 and r.json()["version"] == "1.0.0"
    assert "view.notes-board.new" not in command_ids(client)
    assert "view.notes-board" in command_ids(client)
    missing = client.post(
        "/api/plugins/notes-board/rollback", headers=AUTH, json={"version": "9.0.0"}
    )
    assert missing.status_code == 404

    # disabled it offers nothing, enabled it is back
    client.put("/api/plugins/notes-board/enabled", headers=AUTH, json={"enabled": False})
    assert "view.notes-board" not in command_ids(client)
    offered = client.get("/api/ui/plugins", headers=AUTH).json()["plugins"]
    assert "notes-board" not in {m["id"] for m in offered}
    client.put("/api/plugins/notes-board/enabled", headers=AUTH, json={"enabled": True})
    assert "view.notes-board" in command_ids(client)

    # removed: gone with its versions, the audit stays
    assert client.delete("/api/plugins/notes-board", headers=AUTH).status_code == 200
    assert "view.notes-board" not in command_ids(client)
    assert client.get("/api/plugins/notes-board", headers=AUTH).status_code == 404
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        actions = conn.execute(
            text(
                "SELECT c.action, c.to_version, u.name FROM plugin_changes c "
                "LEFT JOIN users u ON u.id = c.user_id "
                "WHERE plugin_key = 'notes-board' ORDER BY c.id"
            )
        ).all()
    engine.dispose()
    assert [a for a, _, _ in actions] == [
        "install",
        "update",
        "rollback",
        "disable",
        "enable",
        "remove",
    ]
    assert all(name for _, _, name in actions)  # every change names who made it
    assert len(drain(client, queue)) == 4
    # and it can be installed again
    install(client, manifest())


def test_a_broken_or_foreign_manifest_is_refused_with_its_problems(client: TestClient) -> None:
    r = client.post("/api/plugins", headers=AUTH, json={"id": "X", "version": "1"})
    assert r.status_code == 422
    problems = " ".join(r.json()["detail"]["problems"])
    assert "id" in problems and "version" in problems and "title_key" in problems
    # only the hub registers built-in programs
    r = client.post("/api/plugins", headers=AUTH, json=manifest(builtin=True))
    assert r.status_code == 422 and "builtin" in " ".join(r.json()["detail"]["problems"])
    # a key that is installed takes a new version, not a second install
    assert client.post("/api/plugins", headers=AUTH, json=manifest("chat")).status_code == 409
    # an update must be of the same plug-in
    r = client.post("/api/plugins/chat/versions", headers=AUTH, json=manifest("research", "2.0.0"))
    assert r.status_code == 422
    # the check answers without storing
    r = client.post("/api/plugins/check", headers=AUTH, json=manifest())
    assert r.json() == {"problems": []}
    assert client.get("/api/plugins/notes-board", headers=AUTH).status_code == 404


def test_a_built_in_program_cannot_be_removed_or_switched_off(client: TestClient) -> None:
    r = client.delete("/api/plugins/chat", headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "builtin"
    r = client.put("/api/plugins/chat/enabled", headers=AUTH, json={"enabled": False})
    assert r.status_code == 409
    assert "open.chat" in command_ids(client)


# --- rights only narrow --------------------------------------------------------------------------


def app_ceiling(client: TestClient, application: str) -> dict[str, Any]:
    admin = CurrentUser(id=str(DEV_ADMIN_ID), name="admin", role="admin")

    async def look() -> dict[str, Any]:
        async with client.app.state.sessionmaker() as db:
            return await app_scope.ceiling(db, admin, application)

    return client.portal.call(look)


def research_version(version: str, ceiling: list[str]) -> dict[str, Any]:
    m = manifest("research", version)
    m["title_key"] = "workspace.research"
    m["agent"] = {"tools_ceiling": ceiling}
    return m


def test_a_plugin_narrows_the_ceiling_of_its_program_and_never_widens_it(
    client: TestClient,
) -> None:
    before = client.get("/api/ui/workspaces/research/agent", headers=AUTH).json()["tool_scope"]
    assert before == {"nodes": None, "groups": ["notes", "wiki"], "internet": True}

    # narrower: only the wiki, no web
    r = client.post(
        "/api/plugins/research/versions", headers=AUTH, json=research_version("1.1.0", ["wiki"])
    )
    assert r.status_code == 201, r.text
    after = client.get("/api/ui/workspaces/research/agent", headers=AUTH).json()["tool_scope"]
    assert after == {"nodes": None, "groups": ["wiki"], "internet": False}
    # the one door every task and turn goes through (app_scope, D116) applies it too
    assert app_ceiling(client, "research") == {"nodes": None, "groups": ["wiki"], "internet": False}

    # wider than the contract (proto/agents/research.json): refused, nothing changes
    for wider in (["wiki", "shell"], ["notes", "wiki", "internet", "host"]):
        r = client.post(
            "/api/plugins/research/versions",
            headers=AUTH,
            json=research_version("1.2.0", wider),
        )
        assert r.status_code == 422, r.text
        assert "never add" in " ".join(r.json()["detail"]["problems"])
    assert client.get("/api/plugins/research", headers=AUTH).json()["version"] == "1.1.0"

    # a plug-in that is no program has no contract, so no tools at all
    r = client.post("/api/plugins", headers=AUTH, json=manifest(agent={"tools_ceiling": ["notes"]}))
    assert r.status_code == 422

    # rolled back, the program has its full ceiling again
    client.post("/api/plugins/research/rollback", headers=AUTH, json={"version": "1.0.0"})
    back = client.get("/api/ui/workspaces/research/agent", headers=AUTH).json()["tool_scope"]
    assert back == before


def test_the_persons_grants_still_narrow_what_a_plugin_allows(client: TestClient) -> None:
    _, user = make_user(TEST_DB_URL, "narrowed")
    client.put(
        "/api/ui/profiles/default",
        headers=AUTH,
        json={
            "default": True,
            "grants": [
                {"kind": "workspace", "key": "research", "effect": "allow"},
                {"kind": "tool", "key": "notes", "effect": "deny"},
            ],
        },
    )
    # the plug-in keeps the notes and the web; the profile still takes the notes away
    r = client.post(
        "/api/plugins/research/versions",
        headers=AUTH,
        json=research_version("1.1.0", ["notes", "internet"]),
    )
    assert r.status_code == 201, r.text
    got = client.get("/api/ui/workspaces/research/agent", headers=user).json()
    assert got["tool_scope"]["groups"] == [] and got["denied"] == ["notes"]
    assert got["tool_scope"]["internet"] is True


def test_a_command_never_opens_a_program_the_person_may_not_see(client: TestClient) -> None:
    _, user = make_user(TEST_DB_URL, "plain", role="user")
    sneaky = manifest(
        "sneaky",
        commands=[
            {
                "id": "open.dev.sneaky",
                "label_key": "x",
                "action": {"kind": "open_program", "program": "dev"},
            },
            {"id": "view.sneaky", "label_key": "x", "action": {"kind": "open_view"}},
        ],
    )
    install(client, sneaky)
    assert "open.dev.sneaky" in command_ids(client)  # the admin may open Development
    mine = command_ids(client, user)
    assert "open.dev.sneaky" not in mine and "view.sneaky" in mine


def test_an_admins_version_of_a_built_in_survives_a_start(client: TestClient) -> None:
    client.post(
        "/api/plugins/research/versions", headers=AUTH, json=research_version("1.1.0", ["wiki"])
    )
    client.portal.call(plugin_registry.ensure_builtin, client.app.state.sessionmaker)
    detail = client.get("/api/plugins/research", headers=AUTH).json()
    assert detail["version"] == "1.1.0"
    assert [v["version"] for v in detail["versions"]] == ["1.1.0", "1.0.0"]


@pytest.mark.parametrize(
    ("ceiling", "groups", "internet"),
    [
        (None, ["notes", "wiki"], True),
        (["wiki"], ["wiki"], False),
        (["wiki", "internet", "shell"], ["wiki"], True),
        ([], [], False),
    ],
)
def test_narrow_agent_only_removes(
    ceiling: list[str] | None, groups: list[str], internet: bool
) -> None:
    agent = {"tool_scope": {"nodes": None, "groups": ["notes", "wiki"], "internet": True}}
    scope = plugin_registry.narrow_agent(agent, ceiling)["tool_scope"]
    assert scope["groups"] == groups and scope["internet"] is internet
    # a program with the web off never gets it from a plug-in
    off = {"tool_scope": {"groups": ["wiki"], "internet": False}}
    assert (
        plugin_registry.narrow_agent(off, ["wiki", "internet"])["tool_scope"]["internet"] is False
    )
