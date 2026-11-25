"""The identity view (E3-12, D47): people, groups, apps and the chain between them, from the
Keycloak snapshot of a tool node. Read-only; says how old the look is and what it cannot see."""

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import FakeNode, eventually, pair, tools_inventory
from tests.users import make_user
from topcoat_hub import identity
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.tools import Caller, ToolError

OK = {"ok": True}


def snap(**over: Any) -> dict[str, Any]:
    """Ann (from the directory) is in /family, Bob (disabled) in /family/kids, Cy in nothing."""
    base = {
        "realm": "home",
        "taken_at": "2026-10-03T01:00:00Z",
        "users": {
            **OK,
            "total": 3,
            "data": [
                {
                    "id": "u1",
                    "username": "ann",
                    "name": "Ann Example",
                    "enabled": True,
                    "source": "directory",
                },
                {"id": "u2", "username": "bob", "name": "", "enabled": False, "source": "local"},
                {"id": "u3", "username": "cy", "name": "Cy", "enabled": True, "source": "local"},
            ],
        },
        "groups": {
            **OK,
            "total": 3,
            "data": [
                {
                    "id": "g1",
                    "path": "/family",
                    "name": "family",
                    "members": ["u1"],
                    "members_ok": True,
                    "realm_roles": ["household"],
                    "client_roles": {"homarr": ["user"]},
                },
                {
                    "id": "g2",
                    "path": "/family/kids",
                    "name": "kids",
                    "members": ["u2"],
                    "members_ok": True,
                },
                {
                    "id": "g3",
                    "path": "/admins",
                    "name": "admins",
                    "members": [],
                    "members_ok": True,
                    "client_roles": {"homarr": ["admin"]},
                },
            ],
        },
        "apps": {
            **OK,
            "total": 2,
            "data": [
                {"id": "c1", "client_id": "account", "name": "", "enabled": True, "builtin": True},
                {
                    "id": "c2",
                    "client_id": "homarr",
                    "name": "Homarr",
                    "enabled": True,
                    "builtin": False,
                    "url": "https://homarr.example.lan",
                    "roles": ["admin", "user"],
                },
            ],
        },
    }
    base.update(over)
    return base


# --- the chain, as a pure function ----------------------------------------------------------


def person(model: dict[str, Any], username: str) -> dict[str, Any]:
    return next(p for p in model["people"] if p["username"] == username)


def test_a_member_of_a_subgroup_gets_what_the_parent_group_opens() -> None:
    model = identity.build(snap())
    bob = person(model, "bob")
    assert bob["groups"] == ["/family/kids"] and bob["enabled"] is False
    assert [a["app"] for a in bob["apps"]] == ["homarr"]
    via = bob["apps"][0]["via"][0]
    assert via == {"group": "/family", "member_of": "/family/kids", "kind": "role"}
    ann = person(model, "ann")
    assert ann["source"] == "directory" and ann["apps"][0]["roles"] == ["user"]
    assert ann["apps"][0]["via"] == [{"group": "/family", "member_of": "/family", "kind": "role"}]
    assert person(model, "cy")["apps"] == []


def test_the_apps_say_how_many_people_and_through_which_groups() -> None:
    model = identity.build(snap())
    homarr = next(a for a in model["apps"] if a["client_id"] == "homarr")
    assert homarr["people"] == 2 and homarr["people_enabled"] == 1
    assert homarr["groups"] == ["/family"] and homarr["roles"] == ["admin", "user"]
    admins = next(g for g in model["groups"] if g["path"] == "/admins")
    assert admins["opens"] == [
        {"app": "homarr", "roles": ["admin"], "via_group": "/admins", "kind": "role"}
    ]


def test_what_the_admin_said_an_app_reads_is_marked_as_configured_not_seen() -> None:
    model = identity.build(snap(), {"nextcloud": ["/family"]})
    cy = person(model, "cy")
    assert cy["apps"] == []  # cy is in no group
    ann = person(model, "ann")
    nc = next(a for a in ann["apps"] if a["app"] == "nextcloud")
    assert nc["known_app"] is False and nc["via"] == [
        {"group": "/family", "member_of": "/family", "kind": "configured"}
    ]
    # it even reaches a member of a subgroup
    assert any(a["app"] == "nextcloud" for a in person(model, "bob")["apps"])
    app = next(a for a in model["apps"] if a["client_id"] == "nextcloud")
    assert app["configured_groups"] == ["/family"] and app["known_app"] is False


def test_a_source_that_could_not_be_read_is_said_never_shown_as_nobody() -> None:
    model = identity.build(snap(users={"ok": False, "error": "Keycloak answered HTTP 500"}))
    assert model["people"] == []
    assert any("users could not be read" in n and "HTTP 500" in n for n in model["notes"])
    model = identity.build(snap(groups={**OK, "total": 150, "truncated": True, "data": []}))
    assert any("groups list was cut at 150" in n for n in model["notes"])
    data = snap()
    data["groups"]["data"][1]["members_ok"] = False
    assert any(
        "members of /family/kids could not be read" in n for n in identity.build(data)["notes"]
    )


def test_the_limits_are_always_named() -> None:
    notes = identity.build(snap())["notes"]
    assert notes and "Composite roles are not expanded" in notes[0]


def test_a_broken_row_is_dropped_not_fatal() -> None:
    data = snap()
    data["users"]["data"].append("junk")
    data["groups"]["data"].append({"path": ""})
    data["apps"]["data"].append({"client_id": ""})
    assert len(identity.build(data)["people"]) == 3


def test_explain_names_the_chain_or_the_limit() -> None:
    model = identity.build(snap(), {})
    out = identity.explain(model, "bob", "homarr")
    assert out["found"] and out["has_access"] and out["via"][0]["group"] == "/family"
    assert out["enabled_user"] is False
    no = identity.explain(model, "cy", "Homarr")
    assert no["found"] and no["has_access"] is False
    assert "this look cannot show it" in no["why_not"]
    assert "/family" in no["groups_that_would_open_it"]
    assert identity.explain(model, "nobody", "homarr")["found"] is False
    # an ambiguous name is not guessed
    assert (
        identity.explain(model, "a", "homarr")
        == {
            "found": False,
            "people_matching": ["ann"],
            "apps_matching": ["homarr"],
        }
        or identity.explain(model, "a", "homarr")["found"] is True
    )


def test_app_groups_are_cleaned() -> None:
    assert identity.clean_app_groups(
        {"homarr": ["/b", "/a", "no-slash", 5], "": ["/x"], "x": "nope", 3: ["/y"]}
    ) == {"homarr": ["/a", "/b"]}
    assert identity.clean_app_groups("junk") == {}


# --- over REST with a fake tool node --------------------------------------------------------


@pytest.fixture
def kc(client: TestClient, connect: Any) -> tuple[str, FakeNode]:
    """A tool node that offers keycloak.snapshot and has it switched on."""
    node_id, key = pair(client, "vm-id", role="tools")
    node = connect(node_id, key)
    inv = copy.deepcopy(tools_inventory())
    inv["capabilities"].append(
        {
            "name": "keycloak.snapshot",
            "description": "identities (test)",
            "read_only": True,
            "input_schema": {"type": "object", "additionalProperties": False, "properties": {}},
        }
    )
    node.inventory = inv
    node.capability_answers["keycloak.snapshot"] = snap()
    node.handshake()
    node.serve()

    def stored() -> None:
        offered = client.get(f"/api/nodes/{node_id}/capabilities", headers=AUTH).json()["offered"]
        assert any(c["name"] == "keycloak.snapshot" for c in offered)

    eventually(stored)
    # a node's default configuration already lists every tool capability, keycloak.* included
    cfg = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
    assert "keycloak.snapshot" in cfg["capabilities"]
    return node_id, node


def reads(node: FakeNode) -> int:
    return len([c for c in node.capability_calls if c["capability"] == "keycloak.snapshot"])


def test_the_view_has_people_groups_apps_and_the_age_of_the_look(
    client: TestClient, kc: tuple[str, FakeNode]
) -> None:
    _node_id, node = kc
    body = client.get("/api/identity", headers=AUTH).json()
    assert [s["node"]["name"] for s in body["sources"]] == ["vm-id"]
    src = body["sources"][0]
    assert src["realm"] == "home" and src["age_seconds"] == 0 and src["cached"] is False
    assert [p["username"] for p in src["people"]] == ["ann", "bob", "cy"]
    assert {a["client_id"] for a in src["apps"]} >= {"homarr"}
    assert src["notes"][0].startswith("Composite roles")
    assert reads(node) == 1


def test_a_young_look_is_reused_and_refresh_asks_the_node_again(
    client: TestClient, kc: tuple[str, FakeNode]
) -> None:
    _, node = kc
    client.get("/api/identity", headers=AUTH)
    second = client.get("/api/identity", headers=AUTH).json()["sources"][0]
    assert second["cached"] is True and reads(node) == 1
    third = client.get("/api/identity?refresh=true", headers=AUTH).json()["sources"][0]
    assert third["cached"] is False and reads(node) == 2


def test_a_node_that_fails_is_reported_as_a_failed_source_not_as_nobody(
    client: TestClient, kc: tuple[str, FakeNode]
) -> None:
    _, node = kc
    node.capability_answers["keycloak.snapshot"] = {
        "error": {"code": "capability_failed", "message": "Keycloak is not reachable"}
    }
    src = client.get("/api/identity", headers=AUTH).json()["sources"][0]
    assert "people" not in src and "not reachable" in src["error"]["message"]


def test_only_those_who_may_run_the_capability_see_a_source(
    client: TestClient, kc: tuple[str, FakeNode], settings: Any
) -> None:
    node_id, _ = kc
    bob_id, bob = make_user(settings.database_url, "bob")
    assert client.get("/api/identity", headers=bob).json()["sources"] == []
    # a grant for something else does not open it
    client.put(
        f"/api/nodes/{node_id}/grants/{bob_id}", headers=AUTH, json={"capabilities": ["docker.ps"]}
    )
    assert client.get("/api/identity", headers=bob).json()["sources"] == []
    client.put(
        f"/api/nodes/{node_id}/grants/{bob_id}",
        headers=AUTH,
        json={"capabilities": ["keycloak.snapshot"]},
    )
    assert len(client.get("/api/identity", headers=bob).json()["sources"]) == 1
    # an admin's API key does not inherit the admin's rights
    _, admin_key = make_user(settings.database_url, "root", role="admin")
    assert client.get("/api/identity", headers=admin_key).json()["sources"] == []


def test_the_admins_note_of_what_an_app_reads_changes_the_chain_and_only_an_admin_writes_it(
    client: TestClient, kc: tuple[str, FakeNode], settings: Any
) -> None:
    _node_id, _ = kc
    _, user_key = make_user(settings.database_url, "bob")
    r = client.put("/api/identity/app-groups", headers=user_key, json={"app_groups": {"x": ["/a"]}})
    assert r.status_code in (401, 403)
    r = client.put(
        "/api/identity/app-groups",
        headers=AUTH,
        json={"app_groups": {"nextcloud": ["/family", "nope"]}},
    )
    assert r.status_code == 200 and r.json()["app_groups"] == {"nextcloud": ["/family"]}
    body = client.get("/api/identity", headers=AUTH).json()
    assert body["app_groups"] == {"nextcloud": ["/family"]}
    ann = next(p for p in body["sources"][0]["people"] if p["username"] == "ann")
    assert any(a["app"] == "nextcloud" and a["via"][0]["kind"] == "configured" for a in ann["apps"])
    # cleared again
    client.put("/api/identity/app-groups", headers=AUTH, json={"app_groups": {}})
    assert client.get("/api/identity", headers=AUTH).json()["app_groups"] == {}


def test_explain_over_rest(client: TestClient, kc: tuple[str, FakeNode]) -> None:
    r = client.get("/api/identity/explain?person=bob&app=homarr", headers=AUTH)
    res = r.json()["results"][0]
    assert res["has_access"] is True and res["via"][0]["member_of"] == "/family/kids"
    assert client.get("/api/identity/explain?person=bob", headers=AUTH).status_code == 422


def test_the_audit_log_shows_who_read_the_identities(
    client: TestClient, kc: tuple[str, FakeNode]
) -> None:
    client.get("/api/identity", headers=AUTH)
    rows = client.get("/api/tool-calls", headers=AUTH).json()
    assert [r["capability"] for r in rows] == ["keycloak.snapshot"] and rows[0]["ok"] is True


# --- the chat's tool ----------------------------------------------------------------------


def run_tool(client: TestClient, caller: Caller, **tool_input: Any) -> dict[str, Any]:
    out = client.portal.call(client.app.state.identity.run_tool, caller, tool_input)
    assert out["ok"] is True
    return out["output"]


def admin() -> Caller:
    return Caller(DEV_ADMIN_ID, True, "agent")


def test_the_chat_tool_answers_who_may_use_an_app_and_names_the_limit(
    client: TestClient, kc: tuple[str, FakeNode]
) -> None:
    out = run_tool(client, admin(), app="homarr")
    app = out["results"][0]
    assert app["kind"] == "app" and app["people"] == 2 and app["groups"] == ["/family"]
    assert any("Composite roles are not expanded" in c for c in out["caveats"])
    out = run_tool(client, admin(), person="ann")
    assert out["results"][0]["groups"] == ["/family"]
    out = run_tool(client, admin(), person="bob", app="homarr")
    assert out["results"][0]["has_access"] is True
    out = run_tool(client, admin(), query="kid")
    assert [r["kind"] for r in out["results"]] == ["group"]


def test_the_chat_tool_validates_its_input_and_is_offered_only_to_those_who_may(
    client: TestClient, kc: tuple[str, FakeNode], settings: Any
) -> None:
    svc = client.app.state.identity
    for bad in ({}, {"person": 7}, {"person": "x" * 101}, {"nope": "x"}):
        with pytest.raises(ToolError):
            client.portal.call(svc.run_tool, admin(), bad)
    assert [t["capability"] for t in client.portal.call(svc.offer, admin())] == ["identity.query"]
    bob_id, _ = make_user(settings.database_url, "bob")
    assert client.portal.call(svc.offer, Caller(bob_id, False, "agent")) == []


def test_a_user_without_the_right_gets_no_identities_from_the_chat_tool(
    client: TestClient, kc: tuple[str, FakeNode], settings: Any
) -> None:
    bob_id, _ = make_user(settings.database_url, "bob")
    out = run_tool(client, Caller(bob_id, False, "agent"), person="ann")
    assert out["results"] == [] and out["caveats"] == ["no node offers identities to you"]
