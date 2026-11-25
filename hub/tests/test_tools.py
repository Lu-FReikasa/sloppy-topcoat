"""Tool nodes (E2-04): pairing with role tools, capabilities, grants (D20), audit (D15)."""

import json
import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import FakeNode, eventually, pair, tools_inventory
from tests.users import make_user

PS_OUTPUT = {"containers": [{"name": "web", "state": "running"}], "total": 1, "truncated": False}


def tools_node(
    client: TestClient, connect: Any, name: str = "vm-docker", **inventory: Any
) -> tuple[str, FakeNode]:
    """A paired, connected tool node whose inventory the hub has stored."""
    node_id, key = pair(client, name, role="tools")
    node = connect(node_id, key)
    node.inventory = tools_inventory(**inventory)
    node.capability_answers["docker.ps"] = PS_OUTPUT
    node.handshake()
    node.serve()

    def stored() -> None:
        assert client.get(f"/api/nodes/{node_id}/capabilities", headers=AUTH).json()["offered"]

    eventually(stored)
    return node_id, node


def set_config(client: TestClient, node_id: str, change: Any) -> None:
    cfg = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
    change(cfg)
    r = client.put(f"/api/nodes/{node_id}/config", headers=AUTH, json=cfg)
    assert r.status_code == 200, r.text


def run(client: TestClient, node_id: str, cap: str, body: dict | None = None, who=AUTH):
    return client.post(
        f"/api/nodes/{node_id}/capabilities/{cap}", headers=who, json={"input": body or {}}
    )


def audit_rows(node_id: str) -> list[dict[str, Any]]:
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT capability, ok, code, via, output_bytes, output_excerpt, user_id "
                "FROM tool_calls WHERE node_id = :n ORDER BY id"
            ),
            {"n": node_id},
        ).mappings()
        out = [dict(r) for r in rows]
    engine.dispose()
    return out


def grant(client: TestClient, node_id: str, user_id: uuid.UUID, caps: list[str]) -> None:
    r = client.put(
        f"/api/nodes/{node_id}/grants/{user_id}", headers=AUTH, json={"capabilities": caps}
    )
    assert r.status_code == 200, r.text


# --- pairing and what the node offers ------------------------------------------------


def test_a_node_paired_as_tools_gets_a_tools_config(client: TestClient) -> None:
    node_id, _ = pair(client, "vm-docker", role="tools")
    cfg = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
    assert cfg["roles"] == ["tools"] and cfg["runner"] == {"type": "none"}
    assert cfg["allowed_actions"] == ["capability.invoke"]
    assert "docker.logs" in cfg["capabilities"] and "compose.file" in cfg["capabilities"]
    assert cfg["yield"] == {"enabled": False} and cfg["pressure"]["enabled"] is False
    # an ordinary node is unchanged
    other, _ = pair(client, "node-gpu")
    assert client.get(f"/api/nodes/{other}", headers=AUTH).json()["config"]["roles"] == ["gpu"]


def test_the_pair_request_rejects_an_unknown_role(client: TestClient) -> None:
    code = client.post("/api/pairing-codes", headers=AUTH).json()["code"]
    body = {"code": code, "name": "x", "os": "linux", "arch": "amd64", "public_key": "A" * 43 + "="}
    assert client.post("/api/node/pair", json={**body, "role": "root"}).status_code == 400


def test_capabilities_offered_enabled_and_usable(client: TestClient, connect) -> None:
    node_id, _ = tools_node(client, connect)
    d = client.get(f"/api/nodes/{node_id}/capabilities", headers=AUTH).json()
    assert {c["name"] for c in d["offered"]} == {
        "system.info",
        "docker.ps",
        "docker.logs",
        "docker.restart",
    }
    assert all("input_schema" in c and "description" in c for c in d["offered"])
    assert "docker.ps" in d["enabled"] and "docker.restart" not in d["enabled"]
    assert set(d["usable"]) == {"system.info", "docker.ps", "docker.logs"}


# --- running a capability --------------------------------------------------------------


def test_the_admin_runs_a_capability_and_it_is_audited(client: TestClient, connect) -> None:
    node_id, node = tools_node(client, connect)
    r = run(client, node_id, "docker.ps", {"all": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["output"] == PS_OUTPUT and body["capability"] == "docker.ps"
    assert node.capability_calls == [{"capability": "docker.ps", "input": {"all": True}}]
    rows = audit_rows(node_id)
    assert [(r["capability"], r["ok"], r["via"]) for r in rows] == [("docker.ps", True, "gui")]
    assert json.loads(rows[0]["output_excerpt"]) == PS_OUTPUT
    calls = client.get("/api/tool-calls", headers=AUTH).json()
    assert (
        calls[0]["capability"] == "docker.ps"
        and calls[0]["user"]
        and calls[0]["node"] == "vm-docker"
    )


def test_a_capability_that_failed_on_the_node_is_a_failed_result_not_an_http_error(
    client: TestClient, connect
) -> None:
    node_id, node = tools_node(client, connect)
    node.capability_answers["docker.ps"] = {
        "error": {"code": "capability_failed", "message": "cannot reach the Docker API"}
    }
    r = run(client, node_id, "docker.ps")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert r.json()["error"]["code"] == "capability_failed"
    row = audit_rows(node_id)[-1]
    assert (row["ok"], row["code"]) == (False, "capability_failed")


def test_what_the_capability_layer_refuses_never_reaches_the_node(
    client: TestClient, connect
) -> None:
    node_id, node = tools_node(client, connect)
    # a capability that changes state is not run here (D15), although the node offers it
    set_config(client, node_id, lambda c: c["capabilities"].append("docker.restart"))
    r = run(client, node_id, "docker.restart", {"container": "web"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "needs_confirmation"
    # not switched on
    set_config(client, node_id, lambda c: c["capabilities"].remove("docker.logs"))
    r = run(client, node_id, "docker.logs", {"container": "web"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "capability_disabled"
    # not offered by the node build
    set_config(client, node_id, lambda c: c["capabilities"].append("files.read"))
    r = run(client, node_id, "files.read", {})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "unknown_capability"
    # the action itself is not allowed on the node
    set_config(client, node_id, lambda c: c["allowed_actions"].remove("capability.invoke"))
    r = run(client, node_id, "docker.ps")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_allowed"
    assert node.capability_calls == []
    assert [r["code"] for r in audit_rows(node_id)] == [
        "needs_confirmation", "capability_disabled", "unknown_capability", "not_allowed",
    ]  # fmt: skip


def test_the_input_is_checked_against_the_schema(client: TestClient, connect) -> None:
    node_id, node = tools_node(client, connect)
    for cap, body in (
        ("docker.logs", {}),  # container is required
        ("docker.logs", {"container": "web", "lines": 501}),
        ("docker.logs", {"container": "web", "follow": True}),  # unknown field
        ("docker.ps", {"all": "yes"}),
    ):
        r = run(client, node_id, cap, body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_input", (cap, body)
    assert node.capability_calls == []


def test_an_offline_node_is_a_409(client: TestClient, connect) -> None:
    node_id, node = tools_node(client, connect)
    node.close()

    def offline() -> None:
        assert client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["connected"] is False

    eventually(offline)
    r = run(client, node_id, "docker.ps")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "node_offline"
    assert audit_rows(node_id)[-1]["code"] == "node_offline"


# --- who may (D20) -----------------------------------------------------------------------


def test_a_user_needs_a_grant_and_learns_nothing_without_one(
    client: TestClient, connect, settings
) -> None:
    node_id, node = tools_node(client, connect)
    uid, who = make_user(settings.database_url, "family")
    # no grant: refused, and the node does not even show up
    r = run(client, node_id, "docker.ps", who=who)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_granted"
    assert client.get(f"/api/nodes/{node_id}/capabilities", headers=who).status_code == 404
    assert node.capability_calls == []
    # one capability granted
    grant(client, node_id, uid, ["docker.ps"])
    assert run(client, node_id, "docker.ps", who=who).status_code == 200
    r = run(client, node_id, "docker.logs", {"container": "web"}, who=who)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_granted"
    d = client.get(f"/api/nodes/{node_id}/capabilities", headers=who).json()
    assert d["usable"] == ["docker.ps"] and d["grants"] == ["docker.ps"]
    # a user's call is audited as theirs, via the API key
    rows = audit_rows(node_id)
    assert [(r["capability"], r["ok"], r["via"], str(r["user_id"])) for r in rows] == [
        ("docker.ps", False, "api", str(uid)),
        ("docker.ps", True, "api", str(uid)),
        ("docker.logs", False, "api", str(uid)),
    ]


def test_a_star_grant_covers_the_read_only_capabilities_only(
    client: TestClient, connect, settings
) -> None:
    node_id, _ = tools_node(client, connect)
    uid, who = make_user(settings.database_url, "family")
    grant(client, node_id, uid, ["*"])
    assert run(client, node_id, "system.info", who=who).status_code == 200
    assert run(client, node_id, "docker.logs", {"container": "web"}, who=who).status_code == 200
    set_config(client, node_id, lambda c: c["capabilities"].append("docker.restart"))
    r = run(client, node_id, "docker.restart", {"container": "web"}, who=who)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "needs_confirmation"
    d = client.get(f"/api/nodes/{node_id}/capabilities", headers=who).json()
    assert "docker.restart" not in d["usable"]


def test_the_read_only_shell_is_off_by_default_and_never_covered_by_a_star_grant(
    client: TestClient, connect, settings
) -> None:
    node_id, key = pair(client, "vm-shell", role="tools")
    node = connect(node_id, key)
    node.inventory = tools_inventory(with_shell=True)
    node.capability_answers["shell.read"] = {"output": {"command": "ls", "output": "x"}}
    node.handshake()
    node.serve()
    eventually(
        lambda: client.get(f"/api/nodes/{node_id}/capabilities", headers=AUTH).json()["offered"]
    )
    body = {"command": "ls /etc"}
    # the node offers it, the default config does not switch it on: nobody runs it, an admin neither
    cfg = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
    assert "shell.read" not in cfg["capabilities"]
    r = run(client, node_id, "shell.read", body)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "capability_disabled"
    # an admin switches it on for this node: the admin runs it
    set_config(client, node_id, lambda c: c["capabilities"].append("shell.read"))
    assert run(client, node_id, "shell.read", body).status_code == 200
    # "*" is every read-only capability EXCEPT the shell; only a grant that names it opens it
    uid, who = make_user(settings.database_url, "family")
    grant(client, node_id, uid, ["*"])
    r = run(client, node_id, "shell.read", body, who=who)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_granted"
    d = client.get(f"/api/nodes/{node_id}/capabilities", headers=who).json()
    assert "shell.read" not in d["usable"] and "system.info" in d["usable"]
    grant(client, node_id, uid, ["*", "shell.read"])
    assert run(client, node_id, "shell.read", body, who=who).status_code == 200
    d = client.get(f"/api/nodes/{node_id}/capabilities", headers=who).json()
    assert "shell.read" in d["usable"]
    # every call, the refused ones too, is audited with its input
    rows = [r for r in audit_rows(node_id) if r["capability"] == "shell.read"]
    assert [(r["ok"], r["code"]) for r in rows] == [
        (False, "capability_disabled"),
        (True, None),
        (False, "not_granted"),
        (True, None),
    ]


def test_an_admins_api_key_is_a_plain_user_here(client: TestClient, connect, settings) -> None:
    node_id, _ = tools_node(client, connect)
    _uid, key = make_user(settings.database_url, "admin-key", role="admin")
    r = run(client, node_id, "docker.ps", who=key)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_granted"


def test_grants_are_admin_only_replace_and_validate(client: TestClient, connect, settings) -> None:
    node_id, _ = tools_node(client, connect)
    uid, who = make_user(settings.database_url, "family")
    url = f"/api/nodes/{node_id}/grants/{uid}"
    assert client.put(url, headers=who, json={"capabilities": ["docker.ps"]}).status_code == 403
    assert client.get(f"/api/nodes/{node_id}/grants", headers=who).status_code == 403
    assert client.get("/api/users", headers=who).status_code == 403
    assert client.put(url, headers=AUTH, json={"capabilities": ["rm -rf"]}).status_code == 422
    assert (
        client.put(url, headers=AUTH, json={"capabilities": ["docker.ps"], "x": 1}).status_code
        == 422
    )
    assert (
        client.put(
            f"/api/nodes/{node_id}/grants/{uuid.uuid4()}", headers=AUTH, json={"capabilities": []}
        ).status_code
        == 404
    )
    grant(client, node_id, uid, ["docker.ps", "docker.logs", "docker.ps"])
    listed = client.get(f"/api/nodes/{node_id}/grants", headers=AUTH).json()
    assert listed == [
        {"user_id": str(uid), "user": "family", "capabilities": ["docker.logs", "docker.ps"]}
    ]
    grant(client, node_id, uid, ["system.info"])  # replaces
    assert client.get(f"/api/nodes/{node_id}/grants", headers=AUTH).json()[0]["capabilities"] == [
        "system.info"
    ]
    grant(client, node_id, uid, [])  # takes everything away
    assert client.get(f"/api/nodes/{node_id}/grants", headers=AUTH).json() == []
    assert run(client, node_id, "system.info", who=who).status_code == 403
    events = client.get(f"/api/nodes/{node_id}/events", headers=AUTH).json()
    assert [e["kind"] for e in events].count("grants_changed") == 3
    users = client.get("/api/users", headers=AUTH).json()
    assert {"id": str(uid), "name": "family", "role": "user"} in users


def test_the_audit_log_is_scoped_by_user(client: TestClient, connect, settings) -> None:
    node_id, _ = tools_node(client, connect)
    a, key_a = make_user(settings.database_url, "anna")
    b, key_b = make_user(settings.database_url, "ben")
    grant(client, node_id, a, ["docker.ps"])
    grant(client, node_id, b, ["docker.ps"])
    run(client, node_id, "docker.ps", who=key_a)
    run(client, node_id, "docker.ps", who=key_b)
    run(client, node_id, "docker.ps")  # the admin
    mine = client.get("/api/tool-calls", headers=key_a).json()
    assert [c["user_id"] for c in mine] == [str(a)]
    assert len(client.get("/api/tool-calls", headers=AUTH).json()) == 3
    assert len(client.get(f"/api/tool-calls?node_id={node_id}&limit=2", headers=AUTH).json()) == 2
    assert (
        client.get("/api/tool-calls", headers={"Authorization": "Bearer nope"}).status_code == 401
    )


def test_the_audit_log_keeps_a_cut_of_the_output_and_never_a_secret(
    client: TestClient, connect, settings
) -> None:
    node_id, node = tools_node(client, connect)
    client.app.state.settings.tool_audit_output_bytes = 200
    node.capability_answers["docker.ps"] = {"log": "x" * 5000}
    r = run(client, node_id, "docker.ps")
    assert r.json()["bytes"] > 5000  # the caller gets everything
    assert len(audit_rows(node_id)[-1]["output_excerpt"]) == 200
    # a node that failed to mask: the audit log refuses to keep it
    node.capability_answers["docker.ps"] = {
        "log": "login ok password=hunter2hunter2 and AKIAIOSFODNN7EXAMPLE"
    }
    run(client, node_id, "docker.ps")
    kept = audit_rows(node_id)[-1]["output_excerpt"]
    assert "AKIAIOSFODNN7EXAMPLE" not in kept and "not kept in the audit log" in kept
    client.app.state.settings.tool_audit_output_bytes = 0
    run(client, node_id, "docker.ps")
    assert audit_rows(node_id)[-1]["output_excerpt"] is None


def test_masked_values_do_not_make_the_audit_withhold_an_output(
    client: TestClient, connect
) -> None:
    node_id, node = tools_node(client, connect)
    node.capability_answers["docker.ps"] = {
        "content": 'services:\n  db:\n    "password": "«masked:secret_key»"\n    TZ: Europe/Berlin'
    }
    run(client, node_id, "docker.ps")
    kept = audit_rows(node_id)[-1]["output_excerpt"]
    assert "not kept in the audit log" not in kept and "Europe/Berlin" in kept


def test_an_oversized_input_is_refused_before_it_reaches_the_node(
    client: TestClient, connect
) -> None:
    node_id, node = tools_node(client, connect)
    r = run(client, node_id, "docker.ps", {"name": "x" * 20000})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_input"
    assert node.capability_calls == []


def test_windows_read_capabilities_are_named_only_like_the_shell() -> None:
    from topcoat_hub.tools import Caller, granted

    user = Caller(user_id=uuid.uuid4(), admin=False, via="api")
    admin = Caller(user_id=uuid.uuid4(), admin=True, via="gui")
    assert granted(user, {"*"}, "system.info")
    for cap in ("windows.events", "windows.power", "shell.read"):
        assert not granted(user, {"*"}, cap)
        assert granted(user, {cap}, cap)
        assert granted(admin, set(), cap)
