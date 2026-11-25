"""Which nodes and tool groups the assistant may use in a conversation (E3-04): chosen per
conversation, remembered as the user's last choice, applied to the decision and to the check, and
never wider than the user's grants (D20)."""

import json

import pytest
from fastapi.testclient import TestClient

from tests import test_chat_tools
from tests.conftest import AUTH, TEST_DB_URL
from tests.test_agent import finish, use
from tests.test_chat import new_conv, use_model
from tests.test_chat_tools import FINDINGS, NativeScripted, ask_with, use_native_model
from tests.users import make_user
from topcoat_hub import tool_groups

world = test_chat_tools.world

AVAILABLE = [
    {"node": "node-a", "capability": "docker.ps"},
    {"node": "node-a", "capability": "docker.logs"},
    {"node": "node-a", "capability": "health.check"},
    {"node": "node-b", "capability": "omada.devices"},
    {"node": "node-b", "capability": "web.search"},
    {"node": "knowledge", "capability": "knowledge.search"},
]


@pytest.fixture(autouse=True)
def _default_model(client: TestClient) -> None:
    use_model(client)


# --- the pieces ---------------------------------------------------------------------------------


def test_a_scope_only_narrows_what_the_grants_allow() -> None:
    caps = lambda xs: [t["capability"] for t in xs]  # noqa: E731
    WEB = AVAILABLE[4]
    assert tool_groups.apply_scope(AVAILABLE, None) == AVAILABLE
    assert tool_groups.apply_scope(AVAILABLE, {"nodes": None, "groups": None}) == AVAILABLE
    assert caps(tool_groups.apply_scope(AVAILABLE, {"nodes": ["node-b"], "groups": None})) == [
        "omada.devices",
        "web.search",
        "knowledge.search",  # the notes are the hub's own view: a server choice keeps them
    ]
    # the web is not one of the servers: choosing servers or groups never removes it
    assert caps(tool_groups.apply_scope(AVAILABLE, {"nodes": None, "groups": ["docker"]})) == [
        "docker.ps",
        "docker.logs",
        "web.search",
    ]
    both = tool_groups.apply_scope(AVAILABLE, {"nodes": ["node-a"], "groups": ["docker"]})
    assert caps(both) == ["docker.ps", "docker.logs", "web.search"]  # from node-b, not chosen
    # no server at all: the web is still there; without a web node nothing is left
    assert tool_groups.apply_scope(AVAILABLE, {"nodes": [], "groups": None}) == [WEB, AVAILABLE[-1]]
    assert tool_groups.apply_scope(AVAILABLE, {"nodes": None, "groups": []}) == [WEB]
    assert tool_groups.apply_scope(AVAILABLE[:2], {"nodes": [], "groups": None}) == []
    assert tool_groups.apply_scope(AVAILABLE, {"nodes": None, "groups": ["notes"]}) == [
        WEB,
        AVAILABLE[-1],
    ]
    # a node or group the user has no grant for is not made available by choosing it
    assert (
        tool_groups.apply_scope(AVAILABLE[:2], {"nodes": ["node-b"], "groups": ["network"]}) == []
    )


def test_a_scope_has_one_form_for_everything() -> None:
    assert tool_groups.clean_scope(None) is None
    assert tool_groups.clean_scope({"nodes": None, "groups": None}) is None
    assert tool_groups.clean_scope("x") is None
    assert tool_groups.clean_scope(
        {"nodes": ["b", "a", "a", 3, ""], "groups": ["web", "bogus", "host", "docker"]}
    ) == {
        "nodes": ["a", "b"],
        "groups": ["docker", "host"],  # the fixed order, unknown keys and "web" dropped
        "internet": None,
    }
    assert tool_groups.clean_scope({"nodes": [], "groups": None}) == {
        "nodes": [],
        "groups": None,
        "internet": None,
    }


def test_the_internet_is_its_own_switch() -> None:
    caps = lambda xs: [t["capability"] for t in xs]  # noqa: E731
    # only the switch: it is a scope of its own, "off" removes the web and nothing else
    assert tool_groups.clean_scope({"internet": False}) == {
        "nodes": None,
        "groups": None,
        "internet": False,
    }
    assert tool_groups.clean_scope({"internet": "yes"}) is None  # not a boolean: ignored
    off = tool_groups.apply_scope(AVAILABLE, {"internet": False})
    assert caps(off) == [
        "docker.ps",
        "docker.logs",
        "health.check",
        "omada.devices",
        "knowledge.search",
    ]
    assert tool_groups.apply_scope(AVAILABLE, {"internet": True}) == AVAILABLE
    # "off" with no server chosen: nothing
    assert tool_groups.apply_scope(AVAILABLE, {"nodes": [], "internet": False}) == [AVAILABLE[-1]]
    assert tool_groups.apply_scope(AVAILABLE, {"nodes": [], "groups": [], "internet": False}) == []
    assert tool_groups.internet_on(None) and tool_groups.internet_on({"nodes": []})
    assert not tool_groups.internet_on({"internet": False})
    # the scope a turn runs with: the conversation's own value, else the user's default
    assert tool_groups.resolve_scope(None, False) == {
        "nodes": None,
        "groups": None,
        "internet": False,
    }
    assert tool_groups.resolve_scope({"internet": True}, False)["internet"] is True
    assert tool_groups.resolve_scope({"nodes": ["a"]}, True) == {
        "nodes": ["a"],
        "groups": None,
        "internet": True,
    }
    assert not tool_groups.restricts_servers({"internet": False})
    assert tool_groups.restricts_servers({"nodes": []})


def test_the_hub_picks_one_node_for_the_web() -> None:
    two = [
        {"node": "search-b", "capability": "web.search"},
        {"node": "search-b", "capability": "web.fetch"},
        {"node": "search-a", "capability": "web.search"},
        {"node": "node-a", "capability": "docker.ps"},
    ]
    assert tool_groups.pick_web_node(two) == "search-b"  # most web capabilities
    assert tool_groups.pick_web_node(two[2:]) == "search-a"  # then the name
    assert tool_groups.pick_web_node(two[3:]) is None
    picked = tool_groups.apply_scope(two, {"nodes": ["node-a"]})
    assert [(t["node"], t["capability"]) for t in picked] == [
        ("search-b", "web.search"),
        ("search-b", "web.fetch"),
        ("node-a", "docker.ps"),
    ]  # one web node, whatever servers were chosen


# --- settings -----------------------------------------------------------------------------------


def test_the_user_sees_what_can_be_chosen(client: TestClient, world) -> None:
    tools = client.get("/api/chat/tools", headers=AUTH).json()
    assert [g["key"] for g in tools["groups"]] == ["docker", "host"]
    assert tools["groups"][0]["label"].startswith("Docker containers")
    by_name = {n["name"]: n for n in tools["nodes"]}
    assert by_name["vm-docker"]["groups"] == ["docker", "host"]
    assert by_name["vm-docker"]["capabilities"] == {
        "docker": ["docker.logs", "docker.ps"],
        "host": ["system.info"],
    }
    # a model node has no tools but is still listed, so the user sees every machine
    assert by_name["node-a"]["groups"] == []
    assert by_name["node-a"]["capabilities"] == {}
    assert by_name["node-a"]["state"] == "available"
    assert [g["via"] for g in tools["groups"]] == ["node", "hub+node"]  # host: + the inventory


def test_the_choice_is_per_conversation_and_the_last_one_is_the_start_of_the_next(
    client: TestClient,
    world,
) -> None:
    conv = new_conv(client)
    assert client.get(f"/api/conversations/{conv}", headers=AUTH).json()["tool_scope"] is None
    r = client.patch(
        f"/api/conversations/{conv}",
        headers=AUTH,
        json={"tool_scope": {"nodes": ["vm-docker"], "groups": ["host", "docker"]}},
    )
    assert r.status_code == 200
    assert r.json()["tool_scope"] == {
        "nodes": ["vm-docker"],
        "groups": ["docker", "host"],
        "internet": None,
    }
    # the next conversation starts with the last choice …
    other = client.post("/api/conversations", headers=AUTH, json={}).json()
    assert other["tool_scope"] == {
        "nodes": ["vm-docker"],
        "groups": ["docker", "host"],
        "internet": None,
    }
    # … unless it says otherwise; choosing everything is the form None, and is remembered too
    custom = client.post(
        "/api/conversations", headers=AUTH, json={"tool_scope": {"nodes": None, "groups": ["host"]}}
    ).json()
    assert custom["tool_scope"] == {"nodes": None, "groups": ["host"], "internet": None}
    r = client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"tool_scope": None})
    assert r.json()["tool_scope"] is None
    assert client.post("/api/conversations", headers=AUTH, json={}).json()["tool_scope"] is None
    bad = client.patch(
        f"/api/conversations/{conv}", headers=AUTH, json={"tool_scope": {"groups": ["nonsense"]}}
    )
    assert bad.status_code == 422
    extra = client.patch(
        f"/api/conversations/{conv}", headers=AUTH, json={"tool_scope": {"hosts": []}}
    )
    assert extra.status_code == 422


def test_the_choice_of_one_user_is_not_the_choice_of_another(client: TestClient, world) -> None:
    _, bob = make_user(TEST_DB_URL, "bob")
    conv = new_conv(client)
    client.patch(
        f"/api/conversations/{conv}", headers=AUTH, json={"tool_scope": {"groups": ["host"]}}
    )
    mine = client.post("/api/conversations", headers=bob, json={}).json()
    assert mine["tool_scope"] is None  # bob's last choice, not the admin's
    assert (
        client.patch(
            f"/api/conversations/{conv}", headers=bob, json={"tool_scope": None}
        ).status_code
        == 404
    )


# --- what it does to an answer ------------------------------------------------------------------


def look(capability: str, **args) -> str:
    return json.dumps(
        {
            "action": "tool",
            "node": "vm-docker",
            "capability": capability,
            "input": args,
            "reason": "look",
        }
    )


def test_the_check_only_gets_the_tools_of_the_chosen_groups(client: TestClient, world) -> None:
    _, tnode, _ = world
    tnode.capability_answers["system.info"] = {"hostname": "vm", "uptime_seconds": 1000}
    scripted = use(
        client,
        look("docker.logs", container="web", lines=5),  # not allowed in this conversation
        look("system.info"),  # host group: allowed
        finish("The host is up.", FINDINGS[:1], ""),
    )
    conv = new_conv(client)
    client.patch(
        f"/api/conversations/{conv}", headers=AUTH, json={"tool_scope": {"groups": ["host"]}}
    )
    _, events, _ = ask_with(client, conv, "Prüf mal meine Container", "on")
    assert events[0][1]["tool_scope"] == {"nodes": None, "groups": ["host"], "internet": True}
    # docker.logs was refused as "you cannot use that" and never reached the node
    assert [c["capability"] for c in tnode.capability_calls] == ["system.info"]
    system = scripted.calls[0][0]["content"]
    assert "- vm-docker: system.info" in system and "docker.logs" not in system
    first_obs = scripted.calls[1][-1]["content"]
    assert "you cannot use that" in first_obs
    steps = [e for e, _ in events]
    assert "tools_done" in steps and events[-1][0] in ("done", "memory")


def test_a_scope_without_any_tool_leaves_a_plain_chat(client: TestClient, world) -> None:
    _, _, node = world
    use_native_model(client)
    conv = new_conv(client)
    client.patch(
        f"/api/conversations/{conv}",
        headers=AUTH,
        json={"tool_scope": {"nodes": ["elsewhere"], "groups": []}},
    )
    _, events, _ = ask_with(client, conv, "Warum startet mein Container neu?")
    kinds = [e for e, _ in events]
    assert "tools_start" not in kinds and events[-1][0] in ("done", "memory")
    assert node.tool_requests == []  # no function is offered either: there is nothing to look at
    status, _, r = ask_with(client, conv, "Prüf mal meine Container", "on")
    assert status == 409 and r.json()["detail"]["code"] == "no_capabilities"


def test_the_function_the_model_is_offered_lists_only_the_chosen_groups(
    client: TestClient, world
) -> None:
    _, _, node = world
    use_native_model(client)
    conv = new_conv(client)
    client.patch(
        f"/api/conversations/{conv}", headers=AUTH, json={"tool_scope": {"groups": ["docker"]}}
    )
    ask_with(client, conv, "Erzähl mir etwas über Pinguine.")
    desc = node.tool_requests[0]["tools"][0]["function"]["description"]
    assert "Docker containers" in desc and "host health" not in desc
    system = node.tool_requests[0]["messages"][0]["content"]
    assert "host health" not in system  # the assistant is not told about what it may not use


def test_the_task_keeps_its_scope(client: TestClient, world) -> None:
    use(client, look("system.info"), finish("ok", FINDINGS[:1], ""))
    conv = new_conv(client)
    client.patch(
        f"/api/conversations/{conv}",
        headers=AUTH,
        json={"tool_scope": {"groups": ["host", "docker"]}},
    )
    _, events, _ = ask_with(client, conv, "Prüf mal meine Container", "on")
    done = next(d for e, d in events if e == "tools_done")
    task = client.get(f"/api/agent/tasks/{done['task_id']}", headers=AUTH).json()
    assert task["status"] == "done"
    from sqlalchemy import create_engine, text

    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        scope = conn.execute(
            text("SELECT scope FROM agent_tasks WHERE id = :i"), {"i": done["task_id"]}
        ).scalar()
    engine.dispose()
    assert scope == {"nodes": None, "groups": ["docker", "host"], "internet": True}


_ = NativeScripted  # imported for the fixtures of test_chat_tools


def test_a_node_choice_keeps_topcoat_itself() -> None:
    available = [
        {"node": "node-a", "capability": "docker.ps"},
        {"node": "topcoat", "capability": "topcoat.state"},
    ]
    scope = {"nodes": ["node-a"], "groups": None, "internet": False}
    assert [t["capability"] for t in tool_groups.apply_scope(available, scope)] == [
        "docker.ps",
        "topcoat.state",
    ]
    only_other = {"nodes": [], "groups": ["docker"], "internet": False}
    assert tool_groups.apply_scope(available, only_other) == []


def test_a_node_choice_does_not_take_the_hub_views_away() -> None:
    both = [
        *AVAILABLE,
        {"node": "github", "capability": "github.read"},
        {"node": "topcoat", "capability": "topcoat.state"},
    ]
    kept = tool_groups.apply_scope(both, {"nodes": ["node-b"], "groups": None})
    assert {t["node"] for t in kept} == {"node-b", "github", "topcoat", "knowledge"}
    # the group switch still takes them away
    only_docker = tool_groups.apply_scope(
        both, {"nodes": None, "groups": ["docker"], "internet": False}
    )
    assert {t["node"] for t in only_docker} == {"node-a"}


# --- incognito and the topic-based tool list -----------------------------------------------------


def test_incognito_only_switches_the_notes_off() -> None:
    assert tool_groups.clean_scope({"memory": False}) == {
        "nodes": None,
        "groups": None,
        "internet": None,
        "memory": False,
    }
    assert tool_groups.clean_scope({"memory": None}) is None  # the user's setting holds
    assert tool_groups.clean_scope({"memory": "no"}) is None
    assert tool_groups.memory_on(None, True) is True
    assert tool_groups.memory_on({"memory": False}, True) is False
    # a conversation can never switch on what the user switched off
    assert tool_groups.memory_on({"memory": True}, False) is False
    assert tool_groups.resolve_scope({"memory": False}, True)["memory"] is False


def test_incognito_is_per_conversation_and_not_the_start_of_the_next(
    client: TestClient, world
) -> None:
    a = new_conv(client)
    client.patch(f"/api/conversations/{a}", headers=AUTH, json={"tool_scope": {"memory": False}})
    assert client.get(f"/api/conversations/{a}", headers=AUTH).json()["tool_scope"] == {
        "nodes": None,
        "groups": None,
        "internet": None,
        "memory": False,
    }
    b = new_conv(client)
    assert client.get(f"/api/conversations/{b}", headers=AUTH).json()["tool_scope"] is None


def test_a_clear_topic_shortens_the_tool_list_and_nothing_else_does() -> None:
    both = [
        *AVAILABLE,
        {"node": "node-a", "capability": "journal.logs"},
        {"node": "node-a", "capability": "shell.run"},
        {"node": "github", "capability": "github.read"},
        {"node": "topcoat", "capability": "topcoat.state"},
    ]
    scope, keys = tool_groups.auto_scope("Welche Docker Container laufen?", None, both)
    assert keys == ["docker", "host", "notes", "topcoat", "shell"]
    assert scope is not None and scope["groups"] == keys
    caps = {t["capability"] for t in tool_groups.apply_scope(both, scope)}
    assert "docker.ps" in caps and "omada.devices" not in caps and "github.read" not in caps
    # no clear topic: everything stays
    assert tool_groups.auto_scope("Wie geht es dir?", None, both) == (None, [])
    # the user's own choice of groups is never touched
    chosen = {"nodes": None, "groups": ["host"], "internet": None}
    assert tool_groups.auto_scope("Docker Container?", chosen, both) == (chosen, [])
    # a topic nothing looks at live: not narrowed (the run then says what it cannot look at)
    assert tool_groups.auto_scope("Wie ist mein Keycloak?", None, AVAILABLE) == (None, [])
    # the other settings of the scope survive
    keep, _ = tool_groups.auto_scope("Docker?", {"internet": False, "memory": False}, both)
    assert keep is not None and keep["internet"] is False and keep["memory"] is False


def test_a_chat_check_about_containers_is_not_offered_the_network_tools(
    client: TestClient, connect, world
) -> None:
    from tests.test_tools import tools_node

    tools_node(client, connect, "vm-net", extra=("omada.devices",))
    scripted = use(client, finish("Alles ruhig.", FINDINGS[:1], ""))
    conv = new_conv(client)
    ask_with(client, conv, "Prüf mal meine Container", "on")
    system = scripted.calls[0][0]["content"]
    assert "docker.logs" in system and "omada.devices" not in system
    # without a topic in the question the whole list is offered
    scripted = use(client, finish("Alles ruhig.", FINDINGS[:1], ""))
    other = new_conv(client)
    ask_with(client, other, "Prüf mal alles", "on")
    assert "omada.devices" in scripted.calls[0][0]["content"]


def test_every_selectable_group_is_accepted_by_the_api():
    """The picker sends every group it lists; one the API did not know made the whole save fail
    (a 422), so checkboxes seemed stuck."""
    from typing import get_args

    from topcoat_hub.api.chat import GroupKey

    assert set(get_args(GroupKey)) == set(tool_groups.SELECTABLE)


def test_incognito_takes_every_server_and_view_away_but_leaves_the_internet() -> None:
    available = [
        {"node": "node-a", "capability": "docker.ps"},
        {"node": "knowledge", "capability": "knowledge.search"},
        {"node": "topcoat", "capability": "topcoat.about"},
        {"node": "search-a", "capability": "web.search"},
    ]
    got = tool_groups.apply_scope(available, {"memory": False})
    assert [t["capability"] for t in got] == ["web.search"]
    off = tool_groups.apply_scope(available, {"memory": False, "internet": False})
    assert off == []
    # without incognito nothing changes
    assert len(tool_groups.apply_scope(available, {"memory": True})) == 4


def test_a_choice_of_groups_keeps_the_report_tools_of_the_run() -> None:
    """research.report.section belongs to no group; losing it to a group choice left a report run
    unable to write a single section (seen live)."""
    from topcoat_hub.tool_groups import apply_scope

    available = [
        {"node": "messages", "capability": "research.report.section"},
        {"node": "knowledge", "capability": "knowledge.search"},
        {"node": "node-a", "capability": "docker.ps"},
    ]
    kept = apply_scope(available, {"groups": ["notes"], "internet": False})
    assert {t["capability"] for t in kept} == {"research.report.section", "knowledge.search"}
