"""The inventory (E3-10, D45): what runs on which node, and how far to trust it.

The node side is the capability ``inventory.snapshot`` (fake node answers canned snapshots); the
hub keeps the last good read per section and never turns a failed look into "nothing runs"."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import FakeNode, eventually, pair, tools_inventory
from tests.users import make_user
from topcoat_hub import inventory
from topcoat_hub.tools import Caller


@pytest.fixture(autouse=True)
def no_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inventory, "REFRESH_COOLDOWN_SECONDS", 0.0)


def container(name: str, state: str = "running", **kw: Any) -> dict[str, Any]:
    return {"name": name, "image": f"{name}:1", "state": state, "restart_count": 0, **kw}


def service(unit: str, active: str = "active", sub: str = "running", **kw: Any) -> dict[str, Any]:
    return {"unit": unit, "active": active, "sub": sub, "description": unit, **kw}


def snapshot(
    containers: list[dict[str, Any]] | dict[str, Any] | None = None,
    services: list[dict[str, Any]] | dict[str, Any] | None = None,
) -> dict[str, Any]:
    def sec(value: Any, default: list[dict[str, Any]]) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        rows = default if value is None else value
        return {"ok": True, "data": rows, "total": len(rows)}

    return {
        "taken_at": "2026-10-02T10:00:00Z",
        "host": {"ok": True, "data": {"hostname": "vm", "os": "Fake Linux 1.0", "kernel": "6.8"}},
        "containers": sec(
            containers, [container("web", compose_project="site", compose_service="web")]
        ),
        "services": sec(services, [service("docker.service")]),
    }


def inventory_node(
    client: TestClient, connect: Any, snap: dict[str, Any] | None = None, name: str = "vm-docker"
) -> tuple[str, FakeNode]:
    node_id, key = pair(client, name, role="tools")
    node = connect(node_id, key)
    node.inventory = tools_inventory(with_inventory=True)
    node.capability_answers["inventory.snapshot"] = snap or snapshot()
    node.handshake()
    node.serve()

    def stored() -> None:
        offered = client.get(f"/api/nodes/{node_id}/capabilities", headers=AUTH).json()["offered"]
        assert any(c["name"] == "inventory.snapshot" for c in offered)

    eventually(stored)
    return node_id, node


def refresh(client: TestClient, node_id: str, who: Any = AUTH):
    return client.post(f"/api/inventory/nodes/{node_id}/refresh", headers=who)


def names(rows: list[dict[str, Any]], gone: bool = False) -> list[str]:
    return [r["name"] for r in rows if r["gone"] == gone]


def audit_calls(node_id: str) -> list[tuple[str, bool, str]]:
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT capability, ok, via FROM tool_calls WHERE node_id = :n ORDER BY id"),
            {"n": node_id},
        ).all()
    engine.dispose()
    return [tuple(r) for r in rows]  # type: ignore[misc]


def test_a_refresh_stores_what_runs_and_says_how_old_it_is(
    client: TestClient, connect: Any
) -> None:
    node_id, node = inventory_node(
        client,
        connect,
        snapshot(
            [
                container("web", compose_project="site", compose_service="web", ports=["80/tcp"]),
                container("db", "exited", compose_project="site", compose_service="db"),
                container("plex", health="healthy"),
            ],
            [
                service("docker.service"),
                service("nginx.service", "inactive", "dead", enabled="enabled"),
            ],
        ),
    )
    body = refresh(client, node_id).json()
    assert body["status"] == "ok" and body["online"] and body["current"] and not body["stale"]
    assert body["age_seconds"] is not None and body["age_seconds"] < 30
    assert names(body["containers"]) == ["db", "plex", "web"]
    assert names(body["services"]) == ["docker.service", "nginx.service"]
    web = next(c for c in body["containers"] if c["name"] == "web")
    assert web["state"] == "running" and web["detail"]["compose_project"] == "site"
    assert web["detail"]["ports"] == ["80/tcp"]
    # compose projects come from the same look: db is down, web is up
    assert body["compose"] == [
        {
            "kind": "compose",
            "name": "site",
            "state": "partial",
            "services": 2,
            "running": 1,
            "containers": ["db", "web"],
        }
    ]
    assert body["host"]["os"] == "Fake Linux 1.0"
    assert {s: v["state"] for s, v in body["sections"].items()} == {
        "host": "ok",
        "containers": "ok",
        "services": "ok",
    }
    # the same through the list endpoint
    listed = client.get("/api/inventory", headers=AUTH).json()
    assert [n["node"]["name"] for n in listed["nodes"]] == ["vm-docker"]
    assert listed["stale_after_seconds"] >= 300
    # a read that works leaves no audit row of its own (it would be one per node per interval) ...
    assert audit_calls(node_id) == []
    # ... and asks the node nothing but the snapshot
    assert [c["capability"] for c in node.capability_calls] == ["inventory.snapshot"]
    assert node.capability_calls[0]["input"] == {}


def test_what_disappears_is_marked_gone_and_comes_back_with_a_new_since(
    client: TestClient, connect: Any
) -> None:
    node_id, node = inventory_node(client, connect, snapshot([container("web"), container("old")]))
    first = refresh(client, node_id).json()
    old_since = next(c for c in first["containers"] if c["name"] == "web")["since"]
    node.capability_answers["inventory.snapshot"] = snapshot([container("web")])
    second = refresh(client, node_id).json()
    assert names(second["containers"]) == ["web"] and names(second["containers"], True) == ["old"]
    gone = next(c for c in second["containers"] if c["name"] == "old")
    assert gone["gone_at"] is not None
    # an unchanged item keeps its "since"
    assert next(c for c in second["containers"] if c["name"] == "web")["since"] == old_since
    # it comes back
    node.capability_answers["inventory.snapshot"] = snapshot([container("web"), container("old")])
    third = refresh(client, node_id).json()
    back = next(c for c in third["containers"] if c["name"] == "old")
    assert not back["gone"] and back["since"] > gone["since"]
    # a state change moves "since"
    node.capability_answers["inventory.snapshot"] = snapshot(
        [container("web", "exited"), container("old")]
    )
    fourth = refresh(client, node_id).json()
    web = next(c for c in fourth["containers"] if c["name"] == "web")
    assert web["state"] == "exited" and web["since"] > old_since


def test_a_section_that_failed_changes_nothing_and_says_so(
    client: TestClient, connect: Any
) -> None:
    node_id, node = inventory_node(client, connect, snapshot([container("web"), container("db")]))
    ok = refresh(client, node_id).json()
    good_read = ok["sections"]["containers"]["read_at"]
    node.capability_answers["inventory.snapshot"] = snapshot(
        containers={"ok": False, "code": "error", "error": "Docker API answered 500"},
        services=[service("docker.service")],
    )
    body = refresh(client, node_id).json()
    # Docker did not answer: the containers are NOT gone, they are last known
    assert names(body["containers"]) == ["db", "web"] and names(body["containers"], True) == []
    sec = body["sections"]["containers"]
    assert sec["state"] == "failed" and "500" in sec["error"] and sec["current"] is False
    assert sec["read_at"] == good_read  # the last good look stays what it was
    assert body["sections"]["services"]["current"] is True
    assert body["status"] == "partial" and body["current"] is False
    assert body["collected_at"] is not None  # something was read


def test_a_list_that_was_cut_does_not_make_the_rest_gone(client: TestClient, connect: Any) -> None:
    node_id, node = inventory_node(client, connect, snapshot([container("a"), container("b")]))
    refresh(client, node_id)
    node.capability_answers["inventory.snapshot"] = snapshot(
        containers={"ok": True, "data": [container("a")], "total": 300, "truncated": True}
    )
    body = refresh(client, node_id).json()
    assert names(body["containers"], True) == []
    assert body["sections"]["containers"]["truncated"] is True


def test_a_node_without_docker_is_not_an_error(client: TestClient, connect: Any) -> None:
    node_id, _ = inventory_node(
        client,
        connect,
        snapshot(containers={"ok": False, "code": "not_configured", "error": "no Docker API"}),
    )
    body = refresh(client, node_id).json()
    assert body["status"] == "ok" and body["sections"]["containers"]["state"] == "n/a"
    assert body["containers"] == [] and body["current"] is True


def test_a_failed_call_is_an_error_not_an_empty_inventory(client: TestClient, connect: Any) -> None:
    node_id, node = inventory_node(client, connect, snapshot([container("web")]))
    refresh(client, node_id)
    node.capability_answers["inventory.snapshot"] = {
        "error": {"code": "capability_failed", "message": "cannot run"}
    }
    body = refresh(client, node_id).json()
    assert body["status"] == "error" and "cannot run" in body["error"] and not body["current"]
    assert names(body["containers"]) == ["web"]  # still there, as last known
    # a failure is audited, whoever the caller
    assert audit_calls(node_id) == [("inventory.snapshot", False, "inventory")]


def test_an_old_look_is_stale_and_an_offline_node_is_last_known(
    client: TestClient, connect: Any
) -> None:
    node_id, node = inventory_node(client, connect)
    refresh(client, node_id)
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE inventory_nodes SET collected_at = now() - interval '2 hours'"),
        )
    engine.dispose()
    body = client.get(f"/api/inventory/nodes/{node_id}", headers=AUTH).json()
    assert body["stale"] is True and body["current"] is False and names(body["containers"])
    node.close()

    def offline() -> None:
        assert client.get(f"/api/inventory/nodes/{node_id}", headers=AUTH).json()["online"] is False

    eventually(offline)
    r = refresh(client, node_id)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "node_offline"
    # the stored look is untouched by the refused refresh
    body = client.get(f"/api/inventory/nodes/{node_id}", headers=AUTH).json()
    assert names(body["containers"]) and body["status"] == "ok"


def test_the_periodic_read_asks_every_connected_node_once_and_retries_failures_sooner(
    client: TestClient, connect: Any
) -> None:
    _node_id, node = inventory_node(client, connect)
    svc = client.app.state.inventory
    assert client.portal.call(svc.collect_due) == 0  # interval 0 in the test settings: off
    svc.settings.inventory_interval_seconds = 300.0
    assert client.portal.call(svc.collect_due) == 1
    assert client.portal.call(svc.collect_due) == 0  # not due again
    assert len(node.capability_calls) == 1
    node.capability_answers["inventory.snapshot"] = {
        "error": {"code": "capability_failed", "message": "boom"}
    }
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(text("UPDATE inventory_nodes SET attempted_at = now() - interval '6 minutes'"))
    engine.dispose()
    assert client.portal.call(svc.collect_due) == 1  # due: ran and failed
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE inventory_nodes SET attempted_at = now() - interval '90 seconds'")
        )
    engine.dispose()
    assert client.portal.call(svc.collect_due) == 1  # a failure is retried after a minute, not 5


def test_the_periodic_read_skips_a_node_that_has_not_switched_it_on(
    client: TestClient, connect: Any
) -> None:
    node_id, node = inventory_node(client, connect)
    svc = client.app.state.inventory
    svc.settings.inventory_interval_seconds = 300.0
    cfg = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
    cfg["capabilities"].remove("inventory.snapshot")
    assert client.put(f"/api/nodes/{node_id}/config", headers=AUTH, json=cfg).status_code == 200
    assert client.portal.call(svc.collect_due) == 0  # not asked, so no refusal is written either
    assert node.capability_calls == []
    assert [c for c in audit_calls(node_id) if c[0] == "inventory.snapshot"] == []
    cfg["capabilities"].append("inventory.snapshot")
    assert client.put(f"/api/nodes/{node_id}/config", headers=AUTH, json=cfg).status_code == 200
    assert client.portal.call(svc.collect_due) == 1  # switched on again: read again


def test_the_node_must_have_the_capability_switched_on(client: TestClient, connect: Any) -> None:
    node_id, node = inventory_node(client, connect)
    cfg = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
    cfg["capabilities"].remove("inventory.snapshot")
    assert client.put(f"/api/nodes/{node_id}/config", headers=AUTH, json=cfg).status_code == 200
    body = refresh(client, node_id).json()
    assert body["status"] == "unavailable" and "not switched on" in body["error"]
    assert node.capability_calls == []  # the hub's own read obeys the node's config


def test_who_sees_the_inventory(client: TestClient, connect: Any) -> None:
    node_id, _ = inventory_node(client, connect)
    refresh(client, node_id)
    bob_id, bob = make_user(TEST_DB_URL, "bob")
    # without a grant the node does not exist for a user
    assert client.get("/api/inventory", headers=bob).json()["nodes"] == []
    assert client.get(f"/api/inventory/nodes/{node_id}", headers=bob).status_code == 404
    assert refresh(client, node_id, bob).status_code == 404
    # a grant for another capability does not open it
    client.put(
        f"/api/nodes/{node_id}/grants/{bob_id}", headers=AUTH, json={"capabilities": ["docker.ps"]}
    )
    assert client.get("/api/inventory", headers=bob).json()["nodes"] == []
    client.put(
        f"/api/nodes/{node_id}/grants/{bob_id}",
        headers=AUTH,
        json={"capabilities": ["inventory.snapshot"]},
    )
    seen = client.get("/api/inventory", headers=bob).json()["nodes"]
    assert [n["node"]["name"] for n in seen] == ["vm-docker"]
    assert refresh(client, node_id, bob).status_code == 200
    # an admin's API key does not inherit the admin's rights
    _, admin_key = make_user(TEST_DB_URL, "admin-2", role="admin")
    assert client.get("/api/inventory", headers=admin_key).json()["nodes"] == []


def run_tool(client: TestClient, caller: Caller, **tool_input: Any) -> dict[str, Any]:
    svc = client.app.state.inventory
    out = client.portal.call(svc.run_tool, caller, tool_input)
    assert out["ok"] is True
    return out["output"]


def admin_caller() -> Caller:
    from topcoat_hub.models import DEV_ADMIN_ID

    return Caller(DEV_ADMIN_ID, True, "agent")


def test_the_chat_tool_finds_what_runs_where_and_says_how_much_to_trust_it(
    client: TestClient, connect: Any
) -> None:
    node_id, node = inventory_node(
        client,
        connect,
        snapshot(
            [container("plex", image="plexinc/pms"), container("web", compose_project="site")],
            [service("docker.service"), service("ssh.service")],
        ),
    )
    refresh(client, node_id)
    out = run_tool(client, admin_caller(), query="plex")
    assert [(i["node"], i["kind"], i["name"], i["state"]) for i in out["items"]] == [
        ("vm-docker", "container", "plex", "running")
    ]
    assert out["nodes"][0]["current"] is True and "caveats" not in out
    assert {i["name"] for i in run_tool(client, admin_caller(), kind="service")["items"]} == {
        "docker.service",
        "ssh.service",
    }
    assert run_tool(client, admin_caller(), kind="compose")["items"][0]["name"] == "site"
    # the node loses a container and the look goes bad: the answer says so
    node.capability_answers["inventory.snapshot"] = snapshot(
        containers={"ok": False, "code": "error", "error": "Docker API answered 500"}
    )
    refresh(client, node_id)
    out = run_tool(client, admin_caller(), query="plex")
    assert [i["name"] for i in out["items"]] == ["plex"]  # last known
    assert any("containers could not be read" in c for c in out["caveats"])
    assert out["nodes"][0]["current"] is False


def test_the_chat_tool_never_calls_a_node_and_validates_its_input(
    client: TestClient, connect: Any
) -> None:
    node_id, node = inventory_node(client, connect)
    refresh(client, node_id)
    before = len(node.capability_calls)
    run_tool(client, admin_caller(), query="web")
    assert len(node.capability_calls) == before
    svc = client.app.state.inventory
    for bad in ({"limit": 0}, {"kind": "vm"}, {"colour": "red"}, {"query": "x" * 101}):
        with pytest.raises(Exception, match=r"."):
            client.portal.call(svc.run_tool, admin_caller(), bad)


def test_the_chat_tool_shows_a_user_only_the_nodes_they_may_see(
    client: TestClient, connect: Any
) -> None:
    node_id, _ = inventory_node(client, connect)
    refresh(client, node_id)
    bob_id, _bob = make_user(TEST_DB_URL, "bob")
    bob = Caller(bob_id, False, "agent")
    svc = client.app.state.inventory
    assert client.portal.call(svc.offer, bob) == []
    out = run_tool(client, bob, query="web")
    assert out["items"] == [] and out["nodes"] == [] and out["caveats"]
    client.put(f"/api/nodes/{node_id}/grants/{bob_id}", headers=AUTH, json={"capabilities": ["*"]})
    assert [t["capability"] for t in client.portal.call(svc.offer, bob)] == ["inventory.list"]
    assert run_tool(client, bob, query="web")["items"]


def test_a_malformed_row_is_dropped_not_fatal(client: TestClient, connect: Any) -> None:
    node_id, _ = inventory_node(
        client,
        connect,
        snapshot([container("web"), {"state": "running"}, "junk", container("web")]),
    )
    body = refresh(client, node_id).json()
    assert names(body["containers"]) == ["web"] and body["status"] == "ok"


def test_a_deleted_node_takes_its_inventory_along(client: TestClient, connect: Any) -> None:
    node_id, node = inventory_node(client, connect)
    refresh(client, node_id)
    node.close()
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM nodes WHERE id = :n"), {"n": uuid.UUID(node_id)})
        left = conn.execute(text("SELECT count(*) FROM inventory_items")).scalar()
    engine.dispose()
    assert left == 0
