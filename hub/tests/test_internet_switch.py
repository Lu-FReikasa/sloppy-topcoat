"""The internet is its own switch in the chat (E3-04): per conversation, its default is a setting,
and it does not depend on which servers or tool groups are chosen. Web search comes from a node the
hub picks among those released for it; "off" removes the web and nothing else."""

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL, alembic_config
from tests.fakenode import FakeNode, eventually, pair, tools_inventory
from tests.test_agent import finish, use
from tests.test_chat import new_conv, sent_messages, use_model
from tests.test_chat_tools import FINDINGS, ask_with, use_native_model
from tests.test_chat_tools import world as world
from tests.test_tools import grant
from tests.users import make_user


@pytest.fixture(autouse=True)
def _default_model(client: TestClient) -> None:
    use_model(client)


def search_node(client: TestClient, connect: Any, name: str, caps=("web.search", "web.fetch")):
    """A tool node that offers only the web capabilities (no server)."""
    node_id, key = pair(client, name, role="tools")
    node: FakeNode = connect(node_id, key)
    inv = tools_inventory()
    inv["capabilities"] = [
        {
            "name": c,
            "description": f"{c} (test)",
            "read_only": True,
            "input_schema": {"type": "object", "additionalProperties": True},
        }
        for c in caps
    ]
    node.inventory = inv
    node.capability_answers["web.search"] = {"query": "q", "count": 0, "results": []}
    node.capability_answers["web.fetch"] = {"url": "https://example.org/", "text": "x" * 600}
    node.handshake()
    node.serve()
    eventually(
        lambda: client.get(f"/api/nodes/{node_id}/capabilities", headers=AUTH).json()["offered"]
    )
    return node_id, node


def patch_scope(client: TestClient, conv: str, scope: dict | None) -> dict:
    r = client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"tool_scope": scope})
    assert r.status_code == 200, r.text
    return r.json()


def look(node: str, capability: str, **args: Any) -> str:
    return json.dumps(
        {"action": "tool", "node": node, "capability": capability, "input": args, "reason": "look"}
    )


# --- what the user sees -------------------------------------------------------------------------


def test_the_web_is_not_a_server_or_a_group_but_the_internet_names_its_node(
    client: TestClient,
    connect,
    world,
) -> None:
    tools = client.get("/api/chat/tools", headers=AUTH).json()
    assert tools["internet"] == {"node": None}  # nothing offers the web yet
    search_node(client, connect, "search-node")
    tools = client.get("/api/chat/tools", headers=AUTH).json()
    assert tools["internet"] == {"node": "search-node"}
    assert [g["key"] for g in tools["groups"]] == ["docker", "host"]  # no "web" group
    # a web-only node is no server; the model node is listed (without tools)
    assert [n["name"] for n in tools["nodes"]] == ["node-a", "vm-docker"]


def test_a_user_released_only_for_the_web_node_has_the_internet_and_no_server(
    client: TestClient, connect
) -> None:
    node_id, _ = search_node(client, connect, "search-node")
    uid, bob = make_user(TEST_DB_URL, "bob")
    assert client.get("/api/chat/tools", headers=bob).json()["internet"] == {"node": None}
    grant(client, node_id, uid, ["web.search"])
    tools = client.get("/api/chat/tools", headers=bob).json()
    assert tools["internet"] == {"node": "search-node"}
    assert tools["groups"] == [] and tools["nodes"] == []


def test_the_default_is_a_setting_and_the_conversation_can_differ(client: TestClient) -> None:
    settings = client.get("/api/chat/settings", headers=AUTH).json()
    assert settings["user"]["internet"] is True
    r = client.put("/api/chat/settings", headers=AUTH, json={"internet": False})
    assert r.json()["user"]["internet"] is False
    # a save that does not mention it keeps it (like the other optional settings)
    r = client.put("/api/chat/settings", headers=AUTH, json={"temperature": 0.3})
    assert r.json()["user"]["internet"] is False and r.json()["user"]["temperature"] == 0.3
    conv = new_conv(client)
    assert client.get(f"/api/conversations/{conv}", headers=AUTH).json()["tool_scope"] is None
    assert patch_scope(client, conv, {"internet": True})["tool_scope"] == {
        "nodes": None,
        "groups": None,
        "internet": True,
    }
    assert (
        client.put("/api/chat/settings", headers=AUTH, json={"internet": True}).status_code == 200
    )


def test_the_internet_choice_is_not_inherited_by_the_next_conversation(
    client: TestClient,
) -> None:
    conv = new_conv(client)
    patch_scope(client, conv, {"nodes": ["vm-docker"], "groups": ["host"], "internet": False})
    other = client.post("/api/conversations", headers=AUTH, json={}).json()
    # the servers are the last choice; the internet starts as the user's default (null here)
    assert other["tool_scope"] == {"nodes": ["vm-docker"], "groups": ["host"], "internet": None}
    # a scope that only switches the internet off restricts no server: that is what is remembered
    only_internet = new_conv(client)
    patch_scope(client, only_internet, {"internet": False})
    assert client.post("/api/conversations", headers=AUTH, json={}).json()["tool_scope"] is None


# --- what it does to an answer ------------------------------------------------------------------


def decision(node: FakeNode) -> tuple[str, str]:
    """The function description and the system prompt of the first request of the turn."""
    req = node.tool_requests[0]
    return req["tools"][0]["function"]["description"], req["messages"][0]["content"]


def test_deselecting_every_server_leaves_the_web_for_the_model(
    client: TestClient,
    connect,
    world,
) -> None:
    _, _, node = world
    search_node(client, connect, "search-node")
    use_native_model(client)
    conv = new_conv(client)
    patch_scope(client, conv, {"nodes": [], "groups": None})
    ask_with(client, conv, "Was ist neu in llama.cpp?")
    desc, system = decision(node)
    assert "web search" in desc and "Docker containers" not in desc and "host health" not in desc
    assert "search the web" in system


def test_off_removes_the_web_and_keeps_the_servers_and_says_so(
    client: TestClient,
    connect,
    world,
) -> None:
    _, _, node = world
    search_node(client, connect, "search-node")
    use_native_model(client)
    conv = new_conv(client)
    patch_scope(client, conv, {"internet": False})
    _, events, _ = ask_with(client, conv, "Was ist neu in llama.cpp?")
    desc, system = decision(node)
    assert "Docker containers" in desc and "web search" not in desc
    can = system.split("You can: ", 1)[1].split(". ", 1)[0]  # what the assistant is told it can do
    assert "switched Internet off" in system and "search the web" not in can
    meta = events[0][1]
    assert meta["internet"] is False and "internet_node" not in meta
    assert "tool_scope" not in meta  # the servers are not restricted: nothing to announce


def test_off_without_any_server_is_a_plain_chat_that_still_knows_the_switch(
    client: TestClient,
    connect,
    world,
) -> None:
    _, _, node = world
    search_node(client, connect, "search-node")
    use_native_model(client)
    conv = new_conv(client)
    patch_scope(client, conv, {"nodes": [], "groups": [], "internet": False})
    _, events, _ = ask_with(client, conv, "Was ist neu in llama.cpp?")
    assert "tools_start" not in [e for e, _ in events]
    assert node.tool_requests == []  # nothing to look at: no function is offered
    assert "switched Internet off" in sent_messages(node)[0]["content"]
    status, _, r = ask_with(client, conv, "Prüf mal meine Container", "on")
    assert status == 409 and r.json()["detail"]["code"] == "no_capabilities"


def test_the_users_default_applies_until_the_conversation_decides(
    client: TestClient,
    connect,
    world,
) -> None:
    _, _, node = world
    search_node(client, connect, "search-node")
    use_native_model(client)
    client.put("/api/chat/settings", headers=AUTH, json={"internet": False})
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Was ist neu in llama.cpp?")
    assert events[0][1]["internet"] is False
    assert "web search" not in decision(node)[0]
    patch_scope(client, conv, {"internet": True})
    node.tool_requests.clear()
    _, events, _ = ask_with(client, conv, "Und was noch?")
    assert events[0][1]["internet"] is True and events[0][1]["internet_node"] == "search-node"
    assert "web search" in decision(node)[0]


def test_the_check_searches_from_the_web_node_even_with_no_server_chosen(
    client: TestClient,
    connect,
    world,
) -> None:
    _, tnode, _ = world
    _, snode = search_node(client, connect, "search-node")
    use(
        client,
        look("search-node", "web.search", query="llama.cpp release notes"),
        look("vm-docker", "system.info"),  # a server the user deselected: refused
        finish("Found it.", FINDINGS[:0], ""),
    )
    conv = new_conv(client)
    patch_scope(client, conv, {"nodes": []})
    _, events, _ = ask_with(client, conv, "Suche im Web nach llama.cpp", "on")
    assert [c["capability"] for c in snode.capability_calls] == ["web.search"]
    assert tnode.capability_calls == []
    done = next(d for e, d in events if e == "tools_done")
    task = client.get(f"/api/agent/tasks/{done['task_id']}", headers=AUTH).json()
    assert task["status"] == "done"
    refused = [
        s["data"]
        for s in task["steps"]
        if s["kind"] == "observation" and (s["data"].get("tool") or {}).get("ok") is False
    ]
    assert refused and refused[0]["tool"]["code"] == "unknown_tool"


def test_the_hub_picks_the_web_node_not_the_model(
    client: TestClient,
    connect,
    world,
) -> None:
    search_node(client, connect, "search-b")
    _, a = search_node(client, connect, "search-a", caps=("web.search",))
    use_native_model(client)
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Was ist neu in llama.cpp?")
    assert events[0][1]["internet_node"] == "search-b"  # most web capabilities
    # a check lists the web capabilities of that node only
    use(client, finish("ok", [], ""))
    ask_with(client, conv, "Suche im Web", "on")
    assert a.capability_calls == []


def test_the_task_keeps_the_switch(client: TestClient, connect, world) -> None:
    search_node(client, connect, "search-node")
    use(client, look("vm-docker", "system.info"), finish("ok", FINDINGS[:1], ""))
    conv = new_conv(client)
    patch_scope(client, conv, {"internet": False})
    _, events, _ = ask_with(client, conv, "Prüf mal meine Container", "on")
    done = next(d for e, d in events if e == "tools_done")
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        scope = conn.execute(
            text("SELECT scope FROM agent_tasks WHERE id = :i"), {"i": done["task_id"]}
        ).scalar()
    engine.dispose()
    assert scope == {"nodes": None, "groups": None, "internet": False}


# --- data from before the switch ----------------------------------------------------------------


def test_conversations_that_excluded_the_web_keep_the_internet_off(clean_db: str) -> None:
    """Web used to be a tool group: a conversation with groups that lacked it had the web off.
    Migration 0021 turns that into the new switch and removes "web" from the groups."""
    from alembic import command

    cfg = alembic_config()
    command.downgrade(cfg, "0020")
    admin = "00000000-0000-4000-8000-000000000001"  # the seeded dev admin
    ids = {k: str(uuid.uuid4()) for k in ("no_web", "with_web", "nodes_only", "none")}
    scopes = {
        "no_web": {"nodes": None, "groups": ["docker", "host"]},
        "with_web": {"nodes": ["vm-docker"], "groups": ["docker", "web"]},
        "nodes_only": {"nodes": ["vm-docker"], "groups": None},
        "none": None,
    }
    engine = create_engine(TEST_DB_URL)
    try:
        with engine.begin() as conn:
            for key, cid in ids.items():
                conn.execute(
                    text(
                        "INSERT INTO conversations (id, user_id, title, tool_scope) "
                        "VALUES (:i, :u, :t, CAST(:s AS jsonb))"
                    ),
                    {"i": cid, "u": admin, "t": key, "s": json.dumps(scopes[key])},
                )
            conn.execute(
                text(
                    "INSERT INTO user_settings (user_id, tool_scope) VALUES (:u, CAST(:s AS jsonb))"
                ),
                {"u": admin, "s": json.dumps({"nodes": None, "groups": ["host", "web"]})},
            )
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            got = {
                r.title: r.tool_scope
                for r in conn.execute(text("SELECT title, tool_scope FROM conversations"))
            }
            last = conn.execute(text("SELECT tool_scope, internet FROM user_settings")).one()
    finally:
        engine.dispose()
    assert got["no_web"] == {"nodes": None, "groups": ["docker", "host"], "internet": False}
    assert got["with_web"] == {"nodes": ["vm-docker"], "groups": ["docker"]}  # internet: default
    assert got["nodes_only"] == {"nodes": ["vm-docker"], "groups": None}
    assert got["none"] is None
    assert last.tool_scope == {"nodes": None, "groups": ["host"]} and last.internet is True


def test_a_question_in_research_is_researched_not_answered_from_memory(
    client: TestClient,
    connect,
    world,
) -> None:
    """Recherche is the program that reads the web: a real question there is not answered from
    memory and not handed to another program. At "balanced" (the default) the chat offers a
    research of its own instead of a long check (2026-11-13: 31 web searches filled the model's
    context); at "fast" the short check runs (which has the web tools) although the question names
    no web word."""
    from tests.test_agent import tool

    search_node(client, connect, "search-node")
    use(
        client,
        tool("search-node", "web.search", query="systemd history"),
        finish("systemd is the init system of most distributions.", [], ""),
    )
    question = "Bitte arbeite einen ausführlichen Bericht über systemd und seine Geschichte."
    conv = new_conv(client, application="research")
    status, events, _ = ask_with(client, conv, question)
    kinds = [e for e, _ in events]
    assert status == 200 and "tools_start" not in kinds and "handover" not in kinds
    done = next(d for e, d in events if e == "done")
    assert done["message"]["report"]["research_offer"]["question"] == question
    # the offer is a finished answer, also once the run has ended (live 2026-11-20 it was stored
    # as "error": "the answer could not be completed")
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
    assert stored["status"] == "complete" and stored["report"]["research_offer"]

    fast = new_conv(client, application="research")
    client.patch(f"/api/conversations/{fast}", headers=AUTH, json={"quality": "fast"})
    status, events, _ = ask_with(client, fast, question)
    kinds = [e for e, _ in events]
    assert status == 200 and "tools_start" in kinds and "handover" not in kinds
