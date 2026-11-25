"""Which node and model answer, as a table (step 1 of the consolidation).

These tests state what the hub does today with a policy shaped like the one on the production hub:
a fast GPU node first for chat and agent work, the CPU laptop first for background work. They go
through the pieces the call sites use. When the model choice moves into one resolver, the same table
must come out of it (``test_model_resolver.py`` asserts that), so a change in behaviour is a
decision and never an accident."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import FakeNode, eventually, online_node
from tests.policy_helpers import apply_legacy
from topcoat_hub import node_chain
from topcoat_hub.node_policy import AppRow, NodePolicy

# shaped like the production hub's policy was before the table: roles with an explicit model per
# node. The tests turn it into the table (``legacy_policy.convert``, as the hub does at start).
POLICY = {
    "roles": {
        "answer": [
            {"node": "node-amd", "model": "big"},
            {"node": "node-laptop", "model": "small"},
        ],
        "agent": [
            {"node": "node-amd", "model": "big"},
            {"node": "node-laptop", "model": "small"},
        ],
        "background": [
            {"node": "node-laptop", "model": "small"},
            {"node": "node-amd", "model": "big"},
        ],
        "check": [
            {"node": "node-laptop", "model": "small"},
            {"node": "node-amd", "model": "big"},
        ],
    },
}


@pytest.fixture
def world(client: TestClient, connect) -> dict[str, FakeNode]:
    nodes = {
        "node-amd": online_node(client, connect, "node-amd", loaded=["big"]),
        "node-laptop": online_node(client, connect, "node-laptop", loaded=["small"]),
    }
    apply_legacy(client, POLICY)
    return nodes


def _states(client: TestClient) -> dict[str, str]:
    return {n["name"]: n["state"] for n in client.get("/api/nodes", headers=AUTH).json()}


def _is_in_state(client: TestClient, name: str, state: str) -> None:
    assert _states(client)[name] == state


def chat_choice(client: TestClient) -> tuple[str, str, str | None]:
    """What a chat without its own pick does (api.chat._prepare): the first entry of the chat
    route that can answer now, with that node's default model."""
    gateway = client.app.state.gateway

    async def ask() -> tuple[str, str, str | None]:
        chain = await gateway.resolver.chain("answer")
        picked = await node_chain.pick(gateway, chain)
        assert picked is not None
        return picked.node, picked.model, picked.fallback_from

    return client.portal.call(ask)


def agent_choice(client: TestClient) -> str:
    return client.portal.call(lambda: client.app.state.agents.loop.model._model(None))


def care_choice(client: TestClient) -> str | None:
    return client.portal.call(lambda: client.app.state.memory.routed_model("care"))


def test_everything_up(client: TestClient, world) -> None:
    assert chat_choice(client) == ("node-amd", "big", None)
    # the row names both nodes: the node is left to the queue (the laptop has no "big" anyway)
    assert agent_choice(client) == "big@node-amd,node-laptop"
    assert care_choice(client) == "small@node-laptop"  # background work prefers the laptop


def test_the_gpu_node_is_busy_with_a_game(client: TestClient, world) -> None:
    amd = world["node-amd"]
    amd.state = "busy_gaming"
    amd.heartbeat()
    eventually(lambda: _is_in_state(client, "node-amd", "busy_gaming"))
    # chat and agent work move to the next node of the row with the same model; the answer says why
    assert chat_choice(client) == ("node-laptop", "big", "node-amd")
    assert agent_choice(client) == "big@node-amd,node-laptop"  # the busy node is skipped later
    assert care_choice(client) == "small@node-laptop"


def test_the_laptop_is_gone(client: TestClient, world) -> None:
    world["node-laptop"].close()
    eventually(lambda: _is_in_state(client, "node-laptop", "offline"))
    assert chat_choice(client) == ("node-amd", "big", None)
    # the background row lists the laptop first: its pick falls through to the next node
    assert care_choice(client) == "small@node-amd"


def test_a_node_kept_free_of_chat(client: TestClient, world) -> None:
    apply_legacy(client, {**POLICY, "no_chat": ["node-amd"]})
    # the node kept free is not in the row, so the laptop is the first node of it, not a stand-in
    assert chat_choice(client) == ("node-laptop", "small", None)
    assert agent_choice(client) == "small@node-laptop"
    # background work is not chat: the laptop was first for it anyway
    assert care_choice(client) == "small@node-laptop"


def test_nothing_ordered_is_the_hub_default(client: TestClient, world) -> None:
    """A role that was never ordered is the hub's default model."""
    cleared = client.put("/api/scheduling/applications", headers=AUTH, json={"rows": {}})
    assert cleared.status_code == 200
    assert agent_choice(client) == "big"  # the best model of the pools: no default is set
    assert care_choice(client) is None  # the caller falls back to the default model


def test_a_background_request_for_another_model_is_answered_by_the_background_model(
    client: TestClient, world
) -> None:
    """The model of the background role: background work asks for it, whatever it names."""
    state = client.app.state
    state.gateway.set_policy(
        NodePolicy(applications={"background": AppRow(models=("small",), nodes=("node-laptop",))})
    )

    async def route_in_background() -> str:
        route = await state.gateway.preview("big", background=True)
        return route.candidates[0].name

    assert client.portal.call(route_in_background) == "node-laptop"  # "small" is only there
