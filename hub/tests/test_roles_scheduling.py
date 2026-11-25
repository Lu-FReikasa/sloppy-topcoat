"""What the table of applications does to the work, and how the older lists and the old environment
become the table at start (D85). (Before the table, this file tested the model roles.)"""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import eventually, online_node
from tests.policy_helpers import table_of
from tests.test_chat import new_conv, use_model
from tests.test_chat_digest import A_ID, digest_of, two_questions
from tests.test_chat_digest import world as world
from tests.test_segments import ask
from topcoat_hub import legacy_policy
from topcoat_hub.agent_service import GatewayModel
from topcoat_hub.gateway import GatewayError
from topcoat_hub.node_policy import AppRow, NodePolicy, load_policy, parse_policy


def _put(client: TestClient, rows: dict[str, Any]):
    return client.put("/api/scheduling/applications", headers=AUTH, json={"rows": rows})


# --- the table keeps what a pause does not touch ------------------------------------------------


def test_a_pause_keeps_the_table(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    _put(client, {"chat": {"models": ["small"], "nodes": ["node-a"]}})
    r = client.post("/api/scheduling/pause", headers=AUTH, json={"minutes": 5})
    assert r.status_code == 200
    rows = client.get("/api/scheduling/applications", headers=AUTH).json()["rows"]
    assert rows["chat"]["models"] == ["small"]


# --- the work uses it ---------------------------------------------------------------------------


def test_chat_goes_to_the_first_node_of_its_row_and_moves_on_when_that_one_is_busy(
    client: TestClient, connect
) -> None:
    online_node(client, connect, "node-amd", loaded=["small"])
    laptop = online_node(client, connect, "node-laptop", loaded=["big"])
    use_model(client, "small")
    r = _put(client, {"chat": {"models": ["big"], "nodes": ["node-laptop", "node-amd"]}})
    assert r.status_code == 200, r.text
    conv = new_conv(client)
    _, events, _ = ask(client, conv, "eins")
    meta = events[0][1]
    assert (meta["node"], meta["model"], meta["fallback_from"]) == ("node-laptop", "big", None)
    # the laptop is busy: the next node of the row answers, and the answer says why
    laptop.state = "busy_gaming"
    laptop.heartbeat()
    eventually(lambda: _node_state(client, "node-laptop", "busy_gaming"))
    _, events, _ = ask(client, conv, "zwei")
    meta = events[0][1]
    assert meta["node"] == "node-amd"
    assert (meta["fallback_from"], meta["fallback_state"]) == ("node-laptop", "busy_gaming")
    # a conversation with its own pick keeps it
    own = new_conv(client, model="small", node="node-amd")
    _, events, _ = ask(client, own, "drei")
    assert events[0][1]["fallback_from"] is None


def _node_state(client: TestClient, name: str, state: str) -> None:
    nodes = {n["name"]: n["state"] for n in client.get("/api/nodes", headers=AUTH).json()}
    assert nodes[name] == state


def test_the_summary_hangs_on_the_chat_and_the_second_look_is_the_row_check(
    client: TestClient,
    world,
) -> None:
    digester, calls = world
    r = _put(client, {"check": {"models": ["other"], "nodes": ["node-b"]}})
    assert r.status_code == 200, r.text
    conv = two_questions(client)  # answered by node-a with "small"
    assert client.portal.call(lambda: digester.summarise_now(A_ID, uuid.UUID(conv))) == "ok"
    d = digest_of(client, conv)
    assert (d.author_model, d.author_node) == ("small", "node-a")  # where the chat was answered
    assert ("node-a", "summary") in calls and ("node-b", "summary") not in calls
    # the second look is the node of the row, never the writer's model
    assert client.portal.call(lambda: digester.close(A_ID, uuid.UUID(conv))) == "checked"
    assert ("node-b", "check") in calls
    d = digest_of(client, conv)
    assert (d.checker_model, d.checker_node) == ("other", "node-b")


def test_a_summary_never_moves_to_another_node_when_the_chats_node_is_away(
    client: TestClient,
    world,
) -> None:
    digester, calls = world
    conv = two_questions(client)
    real = digester.gateway.call

    async def chat_node_away(target: str, *a: Any, **k: Any) -> Any:
        if target.endswith("@node-a"):
            raise GatewayError(504, "timeout", "the node did not answer in time")
        return await real(target, *a, **k)

    digester.gateway.call = chat_node_away
    assert client.portal.call(lambda: digester.summarise_now(A_ID, uuid.UUID(conv))) == "busy"
    assert ("node-b", "summary") not in calls
    assert digest_of(client, conv) is None


def test_agent_tasks_and_memory_use_the_models_of_their_rows(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b", loaded=["other"])
    state = client.app.state
    model = GatewayModel(state.gateway, state.sessionmaker, state.settings)
    _put(
        client,
        {
            "chat": {"models": ["other", "small"], "nodes": ["node-b", "node-a"]},
            "background": {"models": ["small"], "nodes": ["node-a"]},
        },
    )
    # the row names two nodes that both have the model: either may take the request
    assert client.portal.call(lambda: model._model(None)) == "other@node-b,node-a"
    assert client.portal.call(lambda: model._model("small")) == "small"  # an explicit model wins
    assert client.portal.call(state.memory._model) == "small@node-a"
    # no row for the second look: the caller picks any other model
    assert client.portal.call(lambda: state.memory.routed_model("checker")) is None


# --- the older lists and the old environment become the table -----------------------------------

OLDER = {
    "defaults": {"node-a": "gemma", "node-b": "qwen", "node-c": ""},
    "routes": {
        "chat": ["node-b", "node-a"],
        "care": ["node-a", "node-x"],  # node-x has no default model: nothing to ask it for
        "checker": ["node-b"],
        "summary": ["node-b"],  # a task that no longer exists
    },
}


def convert(raw: Any, env: dict[str, str] | None = None, known: list[str] | None = None):
    return legacy_policy.convert(raw, env or {}, known or ["node-a", "node-b", "node-c"])


def test_a_policy_with_defaults_and_routes_becomes_rows() -> None:
    policy, changed, used = convert(OLDER)
    assert changed and used == []
    chat = policy.applications["chat"]
    # the models in the order of the list, the nodes in the order of the list, the rest after them
    assert chat == AppRow(models=("qwen", "gemma"), nodes=("node-b", "node-a", "all"))
    # every application that had no list of its own has the hub's
    assert policy.applications["research"] == chat and policy.applications["dev"] == chat
    assert policy.applications["background"] == AppRow(
        priority="low", models=("gemma", "same_as_answer"), nodes=("node-a", "all")
    )
    assert policy.applications["check"].models == ("qwen", "same_as_answer")
    assert "api" not in policy.applications
    # nothing of the older keys is written any more
    assert set(policy.to_json()) == {"applications"}


def test_a_list_of_an_application_wins_for_that_application() -> None:
    policy, _, _ = convert(
        {
            "roles": {"answer": [{"node": "node-a", "model": "gemma"}]},
            "app_roles": {"dev": {"answer": [{"node": "node-b", "model": "coder"}]}},
        }
    )
    assert policy.applications["dev"] == AppRow(models=("coder",), nodes=("node-b", "all"))
    assert policy.applications["chat"].models == ("gemma",)


def test_nodes_kept_free_are_left_out_of_the_rows_they_were_kept_free_of() -> None:
    policy, _, _ = convert(
        {
            "roles": {
                "answer": [
                    {"node": "node-a", "model": "gemma"},
                    {"node": "node-b", "model": "qwen"},
                ],
                "background": [{"node": "node-b", "model": "qwen"}],
            },
            "no_chat": ["node-a"],
            "no_background": ["node-c"],
        }
    )
    chat = policy.applications["chat"]
    assert "node-a" not in chat.nodes and "all" not in chat.nodes
    assert chat.models == ("qwen",) and chat.nodes[0] == "node-b" and "node-c" in chat.nodes
    background = policy.applications["background"]
    assert "node-c" not in background.nodes and "all" not in background.nodes


def test_an_application_without_a_list_that_keeps_a_node_free_gets_a_row_of_the_others() -> None:
    policy, _, _ = convert({"no_chat": ["node-a"]})
    assert policy.applications["chat"] == AppRow(nodes=("node-b", "node-c"))
    assert policy.applications["research"] == AppRow(nodes=("node-b", "node-c"))


def test_the_order_and_the_preferred_background_nodes_are_preferences_not_fences() -> None:
    policy, _, _ = convert(
        {
            "order": ["node-b", "node-a"],
            "background_nodes": ["node-c"],
            "roles": {"background": [{"node": "node-a", "model": "gemma"}]},
        }
    )
    assert policy.applications["api"] == AppRow(nodes=("node-b", "node-a", "all"))
    # the preferred background node comes first, the node of the role after it
    assert policy.applications["background"].nodes == ("node-c", "node-a", "all")


@pytest.mark.parametrize(
    "raw",
    [{"routes": {"dreaming": ["node-a"]}}, {"routes": {"chat": ["a", "a"]}}, {"defaults": ["a"]}],
)
def test_a_bad_older_key_is_dropped_and_the_hub_still_starts(raw: dict) -> None:
    policy, changed, _ = convert(raw)
    assert changed and policy.applications == {}
    # and the table itself does not accept the older keys any more
    with pytest.raises(Exception, match="unknown setting"):
        parse_policy(raw)
    assert load_policy(raw) == NodePolicy()


def test_a_policy_that_has_a_table_and_older_lists_keeps_its_rows() -> None:
    policy, changed, _ = convert(
        {
            "applications": {"chat": {"models": ["mine"], "nodes": ["node-c"]}},
            "roles": {"answer": [{"node": "node-a", "model": "gemma"}]},
        }
    )
    assert changed
    assert policy.applications["chat"] == AppRow(models=("mine",), nodes=("node-c",))
    assert policy.applications["research"].models == ("gemma",)  # filled from the lists


def test_a_table_only_policy_is_not_changed() -> None:
    raw = {"applications": {"chat": {"models": ["mine"]}}, "embedding_model": "bge"}
    policy, changed, used = convert(raw)
    assert (changed, used) == (False, [])
    assert policy.to_json() == raw


def test_the_old_environment_becomes_rows_once_and_never_overrides_a_row() -> None:
    env = {
        "TOPCOAT_CHAT_NODE_CHAIN": "node-amd:gemma,node-laptop:qwen",
        "TOPCOAT_GATEWAY_BACKGROUND_NODES": "node-laptop,node-amd",
        "TOPCOAT_GATEWAY_BACKGROUND_MODEL": "qwen",
        "TOPCOAT_REVIEW_MODEL": "qwen@node-laptop",
        "TOPCOAT_MEMORY_MODEL": "qwen@node-laptop",
        "TOPCOAT_GATEWAY_NODE_ORDER": "node-amd,node-laptop",
    }
    nodes = ["node-amd", "node-laptop"]
    policy, changed, used = convert(None, env, nodes)
    assert changed and sorted(used) == sorted(env)
    assert policy.applications["chat"] == AppRow(
        models=("gemma", "qwen"), nodes=("node-amd", "node-laptop", "all")
    )
    assert policy.applications["api"].nodes == ("node-amd", "node-laptop", "all")
    assert policy.applications["check"].models == ("qwen", "same_as_answer")
    # what the admin stored wins
    stored = {"applications": {"chat": {"models": ["mine"], "nodes": ["node-laptop"]}}}
    policy, _, _ = convert(stored, env, nodes)
    assert policy.applications["chat"] == AppRow(models=("mine",), nodes=("node-laptop",))
    # nothing set, nothing changed
    assert convert(None, {}, nodes)[1:] == (False, [])


def test_the_hub_rewrites_an_older_stored_policy_as_the_table_at_start(
    client: TestClient, connect
) -> None:
    from topcoat_hub.models import HubSetting
    from topcoat_hub.node_policy import POLICY_KEY

    state = client.app.state
    online_node(client, connect, "node-a")
    online_node(client, connect, "node-b")

    async def store(value: dict) -> None:
        async with state.sessionmaker() as db:
            row = await db.get(HubSetting, POLICY_KEY)
            if row is None:
                db.add(HubSetting(key=POLICY_KEY, value=value))
            else:
                row.value = value
            await db.commit()

    async def stored() -> dict:
        async with state.sessionmaker() as db:
            row = await db.get(HubSetting, POLICY_KEY)
            assert row is not None
            return row.value

    older = {
        "defaults": {"node-a": "small"},
        "routes": {"chat": ["node-a"]},
        "no_chat": ["node-b"],
        "embedding_model": "bge-m3-q8_0",
    }
    client.portal.call(lambda: store(older))
    client.portal.call(lambda: state.gateway.load_policy({}))
    written = client.portal.call(stored)
    assert set(written) == {"applications", "embedding_model"}
    chat = written["applications"]["chat"]
    # the node kept free is not in the row; the others (this database may hold more) follow
    assert chat["models"] == ["small"] and chat["nodes"][0] == "node-a"
    assert "node-b" not in chat["nodes"] and "all" not in chat["nodes"]
    assert state.gateway.policy.applications["chat"].models == ("small",)
    # the second start finds the table only and writes nothing new
    client.portal.call(lambda: state.gateway.load_policy({}))
    assert client.portal.call(stored) == written


def test_table_of_helper_matches_the_converter() -> None:
    assert table_of({"roles": {"answer": [{"node": "node-amd", "model": "big"}]}})["chat"] == {
        "models": ["big"],
        "nodes": ["node-amd", "all"],
    }
