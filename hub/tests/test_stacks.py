"""Stacks and the inventory's buttons (compose.logs, compose.restart, manual proposals).

The person asks for a restart themselves in the inventory: the hub stores a *manual* proposal (its
own texts, no agent task), shows its exact effect and runs it only on the second, separate click.
Everything else is the proposal machinery of E3-01b: hash, claim, expiry, audit, GUI-only."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import FakeNode, eventually, pair, tools_inventory
from tests.test_inventory import snapshot
from tests.test_tools import set_config
from tests.users import make_user
from topcoat_hub import inventory
from topcoat_hub.models import DEV_ADMIN_ID


@pytest.fixture(autouse=True)
def no_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inventory, "REFRESH_COOLDOWN_SECONDS", 0.0)


@pytest.fixture
def vm(client: TestClient, connect: Any) -> tuple[str, FakeNode]:
    """A tool node with stacks: compose.logs for reading, docker.restart and compose.restart for
    the buttons; its config allows confirmed actions and the hub has proposals on."""
    node_id, key = pair(client, "vm-docker", role="tools")
    node = connect(node_id, key)
    node.inventory = tools_inventory(with_inventory=True, stacks=True)
    node.capability_answers["inventory.snapshot"] = snapshot()
    node.capability_answers["docker.restart"] = {"container": "web", "restarted": True}
    node.capability_answers["compose.restart"] = {"project": "site", "restarted": True}
    node.handshake()
    node.serve()

    def stored() -> None:
        offered = client.get(f"/api/nodes/{node_id}/capabilities", headers=AUTH).json()["offered"]
        assert any(c["name"] == "compose.restart" for c in offered)

    eventually(stored)
    set_config(
        client,
        node_id,
        lambda c: (
            c["allowed_actions"].append("capability.execute"),
            c["capabilities"].extend(["docker.restart", "compose.restart"]),
        ),
    )
    client.app.state.settings.proposals_enabled = True
    return node_id, node


def ask(client: TestClient, node_id: str, capability: str, tool_input: dict[str, Any], who=AUTH):
    return client.post(
        "/api/agent/proposals/manual",
        headers=who,
        json={"node_id": node_id, "capability": capability, "input": tool_input},
    )


def click(client: TestClient, pid: str, what: str = "execute", who=AUTH):
    return client.post(f"/api/agent/proposals/{pid}/{what}", headers=who)


def sql(query: str, **params: Any) -> list[dict[str, Any]]:
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        out = [dict(r) for r in conn.execute(text(query), params).mappings()]
    engine.dispose()
    return out


def test_the_default_capabilities_of_a_tool_node_include_the_stack_logs() -> None:
    from topcoat_hub.protocol import TOOL_CAPABILITIES

    assert "compose.logs" in TOOL_CAPABILITIES
    # a restart is never switched on by default: an admin chooses it per node
    assert "compose.restart" not in TOOL_CAPABILITIES and "docker.restart" not in TOOL_CAPABILITIES


def test_asking_for_a_restart_stores_a_proposal_and_runs_nothing(
    client: TestClient, vm: tuple[str, FakeNode]
) -> None:
    node_id, node = vm
    r = ask(client, node_id, "docker.restart", {"container": "web"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["status"] == "pending" and p["origin"] == "manual" and p["task_id"] is None
    assert p["node"] == "vm-docker" and p["capability"] == "docker.restart"
    assert p["input"] == {"container": "web"} and len(p["hash"]) == 64 and p["expires_at"]
    # the four sentences name the container; the reason says that the person asked
    assert "web" in p["effect"] and "web" in p["who_notices"] and "inventory" in p["reason"]
    assert p["way_back"] and p["evidence"] == []
    assert node.execute_calls == []  # nothing ran
    assert [
        c["capability"] for c in node.capability_calls if c["capability"] != "inventory.snapshot"
    ] == []


def test_the_second_click_runs_it_once_and_the_audit_says_how(
    client: TestClient, vm: tuple[str, FakeNode]
) -> None:
    node_id, node = vm
    p = ask(client, node_id, "compose.restart", {"project": "site"}).json()
    assert "site" in p["effect"] and "one after the other" in p["effect"]
    done = click(client, p["id"])
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "executed" and done.json()["result"]["ok"] is True
    assert node.execute_calls == [
        {
            "capability": "compose.restart",
            "input": {"project": "site"},
            "proposal": p["id"],
            "hash": p["hash"],
        }
    ]
    row = sql(
        "SELECT capability, via, task_id, ok FROM tool_calls WHERE capability = 'compose.restart'"
    )
    assert (
        len(row) == 1 and row[0]["via"] == "proposal" and row[0]["task_id"] is None and row[0]["ok"]
    )
    assert click(client, p["id"]).status_code == 409  # a proposal runs once
    assert len(node.execute_calls) == 1


def test_the_texts_come_in_the_language_of_the_user(
    client: TestClient, vm: tuple[str, FakeNode]
) -> None:
    node_id, _ = vm
    assert client.put("/api/me/language", headers=AUTH, json={"language": "de"}).status_code == 200
    p = ask(client, node_id, "docker.restart", {"container": "web"}).json()
    assert "Du hast" in p["reason"] and "Container web" in p["effect"]
    assert sql("SELECT language FROM users WHERE id = :i", i=DEV_ADMIN_ID)[0]["language"] == "de"


def test_a_refused_manual_proposal_never_runs(client: TestClient, vm: tuple[str, FakeNode]) -> None:
    node_id, node = vm
    p = ask(client, node_id, "docker.restart", {"container": "web"}).json()
    r = click(client, p["id"], "refuse")
    assert r.status_code == 200 and r.json()["status"] == "refused"
    assert click(client, p["id"]).status_code == 409
    assert node.execute_calls == []


def test_asking_twice_leaves_one_open_proposal(
    client: TestClient, vm: tuple[str, FakeNode]
) -> None:
    node_id, node = vm
    first = ask(client, node_id, "docker.restart", {"container": "web"}).json()
    second = ask(client, node_id, "docker.restart", {"container": "web"}).json()
    assert first["id"] != second["id"]
    state = {
        p["id"]: client.get(f"/api/agent/proposals/{p['id']}", headers=AUTH).json()["status"]
        for p in (first, second)
    }
    assert state == {first["id"]: "expired", second["id"]: "pending"}
    assert click(client, first["id"]).status_code == 409  # the replaced one is dead
    assert node.execute_calls == []


@pytest.mark.parametrize(
    ("capability", "tool_input", "status", "code"),
    [
        ("docker.ps", {}, 404, "not_found"),  # a read: nothing to confirm
        ("docker.rm", {"container": "web"}, 404, "not_found"),  # not offered at all
        ("docker.restart", {}, 422, "invalid_input"),
        ("docker.restart", {"container": "web", "force": True}, 422, "invalid_input"),
        ("compose.restart", {"project": 7}, 422, "invalid_input"),
    ],
)
def test_what_is_not_a_change_the_person_may_run_is_refused(
    client: TestClient,
    vm: tuple[str, FakeNode],
    capability: str,
    tool_input: dict[str, Any],
    status: int,
    code: str,
) -> None:
    node_id, node = vm
    r = ask(client, node_id, capability, tool_input)
    assert r.status_code == status, r.text
    assert r.json()["detail"]["code"] == code
    assert node.execute_calls == []


def test_only_the_signed_in_user_asks_and_only_where_proposals_are_on(
    client: TestClient, vm: tuple[str, FakeNode], settings: Any
) -> None:
    node_id, node = vm
    _, admin_key = make_user(settings.database_url, "root", role="admin")
    r = ask(client, node_id, "docker.restart", {"container": "web"}, who=admin_key)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "gui_only"
    client.app.state.settings.proposals_enabled = False
    r = ask(client, node_id, "docker.restart", {"container": "web"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "proposals_off"
    assert node.execute_calls == []


def test_an_api_key_may_not_ask_for_a_change_at_all(
    client: TestClient, vm: tuple[str, FakeNode], settings: Any
) -> None:
    node_id, node = vm
    _, bob = make_user(settings.database_url, "bob")
    # an API key is not a signed-in user: refused before anything is looked at
    assert ask(client, node_id, "docker.restart", {"container": "web"}, who=bob).status_code == 403
    assert node.execute_calls == []


def test_the_inventory_says_what_this_user_may_do_next_to_each_node(
    client: TestClient, vm: tuple[str, FakeNode]
) -> None:
    node_id, _ = vm
    client.post(f"/api/inventory/nodes/{node_id}/refresh", headers=AUTH)
    can = client.get("/api/inventory", headers=AUTH).json()["nodes"][0]["can"]
    assert can["read"] == ["compose.logs", "docker.logs"]
    assert can["change"] == ["compose.restart", "docker.restart"]
    # proposals off: the buttons for a change are not offered, the logs still are
    client.app.state.settings.proposals_enabled = False
    can = client.get(f"/api/inventory/nodes/{node_id}", headers=AUTH).json()["can"]
    assert can["read"] == ["compose.logs", "docker.logs"] and can["change"] == []


def test_a_node_that_does_not_allow_confirmed_actions_offers_no_button(
    client: TestClient, vm: tuple[str, FakeNode]
) -> None:
    node_id, _ = vm
    set_config(client, node_id, lambda c: c["allowed_actions"].remove("capability.execute"))
    client.post(f"/api/inventory/nodes/{node_id}/refresh", headers=AUTH)
    assert client.get("/api/inventory", headers=AUTH).json()["nodes"][0]["can"]["change"] == []


def test_the_stack_logs_are_a_read_the_user_runs_like_any_other(
    client: TestClient, vm: tuple[str, FakeNode]
) -> None:
    node_id, node = vm
    node.capability_answers["compose.logs"] = {"project": "site", "log": "x [web] hi\n", "lines": 1}
    r = client.post(
        f"/api/nodes/{node_id}/capabilities/compose.logs",
        headers=AUTH,
        json={"input": {"project": "site"}},
    )
    assert r.status_code == 200 and r.json()["ok"] is True and "[web]" in r.json()["output"]["log"]
    # a change is not run by this door
    r = client.post(
        f"/api/nodes/{node_id}/capabilities/compose.restart",
        headers=AUTH,
        json={"input": {"project": "site"}},
    )
    assert r.status_code == 403 and r.json()["detail"]["code"] == "needs_confirmation"
    assert node.execute_calls == []


def test_a_manual_proposal_can_be_read_again_by_its_owner_only_and_is_not_on_the_start_list(
    client: TestClient, vm: tuple[str, FakeNode], settings: Any
) -> None:
    node_id, _ = vm
    p = ask(client, node_id, "docker.restart", {"container": "web"}).json()
    assert client.get(f"/api/agent/proposals/{p['id']}", headers=AUTH).json()["status"] == "pending"
    # the person has the card in front of them: the start page's "needs you" list is the agent's
    assert client.get("/api/agent/proposals/pending", headers=AUTH).json() == []
    _, bob = make_user(settings.database_url, "bob")
    assert client.get(f"/api/agent/proposals/{p['id']}", headers=bob).status_code == 404
