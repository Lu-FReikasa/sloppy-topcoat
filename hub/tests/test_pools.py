"""Pools per node (PRG-00 R1): the admin marks which models of a node programs may use."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import FakeNode, online_node
from topcoat_hub.node_policy import NodePolicy, PolicyError, parse_policy


def test_no_pool_means_every_model() -> None:
    policy = NodePolicy()
    assert policy.in_pool("node-amd", "anything")


def test_a_pool_lists_the_available_models_and_survives_a_round_trip() -> None:
    policy = parse_policy({"pools": {"node-amd": {"available": ["a", "b"]}}})
    assert policy.in_pool("node-amd", "a")
    assert not policy.in_pool("node-amd", "c")
    assert policy.in_pool("node-laptop", "c")  # another node: no pool
    assert parse_policy(policy.to_json()).pools == policy.pools


def test_an_empty_pool_is_no_pool() -> None:
    assert parse_policy({"pools": {"node-amd": {"available": []}}}).pools == {}


@pytest.mark.parametrize(
    "bad",
    [
        {"node-amd": ["a"]},  # must be an object
        {"node-amd": {"available": ["a", "a"]}},  # twice
        {"node-amd": {"available": ["a@node-amd"]}},  # a name, not a pin
        {"node-amd": {"available": "a"}},
        {"node-amd": {"models": ["a"]}},  # unknown key
        {"": {"available": ["a"]}},
    ],
)
def test_a_pool_that_does_not_make_sense_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(PolicyError):
        parse_policy({"pools": bad})


@pytest.fixture
def world(client: TestClient, connect) -> dict[str, FakeNode]:
    return {
        "node-amd": online_node(client, connect, "node-amd", loaded=["big", "small"]),
    }


def served(client: TestClient) -> list[str]:
    return [m["id"] for m in client.get("/v1/models", headers=AUTH).json()["data"]]


def test_the_api_lists_models_and_applies_the_pool(client: TestClient, world) -> None:
    r = client.get("/api/scheduling/pools", headers=AUTH)
    assert r.status_code == 200
    node = r.json()["nodes"][0]
    assert node["node"] == "node-amd" and not node["restricted"]
    assert {m["name"]: m["available"] for m in node["models"]} == {"big": True, "small": True}
    assert served(client) == ["big", "small"]

    r = client.put(
        "/api/scheduling/pools",
        headers=AUTH,
        json={"pools": {"node-amd": {"available": ["small"]}}},
    )
    assert r.status_code == 200, r.text
    node = r.json()["nodes"][0]
    assert node["restricted"]
    # the page still shows the whole inventory, with the switch
    assert {m["name"]: m["available"] for m in node["models"]} == {"big": False, "small": True}
    # routing and the model list see the pool only
    assert served(client) == ["small"]


def test_the_page_shows_the_speed_measured_in_real_answers(client: TestClient, world) -> None:
    import uuid

    from topcoat_hub.models import NodeSpeed

    node_id = next(
        n["id"] for n in client.get("/api/nodes", headers=AUTH).json() if n["name"] == "node-amd"
    )

    async def measured() -> None:
        async with client.app.state.sessionmaker() as db:
            db.add(NodeSpeed(node_id=uuid.UUID(node_id), model="big", tg_tps=33.14, tg_samples=5))
            await db.commit()

    client.portal.call(measured)
    models = client.get("/api/scheduling/pools", headers=AUTH).json()["nodes"][0]["models"]
    assert {m["name"]: m["tps"] for m in models} == {"big": 33.1, "small": None}


def test_a_model_outside_the_pool_cannot_be_routed(client: TestClient, world) -> None:
    from topcoat_hub.gateway import GatewayError

    client.put(
        "/api/scheduling/pools",
        headers=AUTH,
        json={"pools": {"node-amd": {"available": ["small"]}}},
    )
    with pytest.raises(GatewayError):
        client.portal.call(lambda: client.app.state.gateway.candidates("big", None))


def test_the_api_refuses_an_unknown_node_and_a_non_admin(client: TestClient, world) -> None:
    r = client.put(
        "/api/scheduling/pools", headers=AUTH, json={"pools": {"nope": {"available": ["a"]}}}
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_pools"
    assert client.get("/api/scheduling/pools").status_code in (401, 403)


def test_the_pool_is_kept_across_other_policy_changes(client: TestClient, world) -> None:
    client.put(
        "/api/scheduling/pools",
        headers=AUTH,
        json={"pools": {"node-amd": {"available": ["small"]}}},
    )
    client.put("/api/scheduling/applications", headers=AUTH, json={"rows": {}})
    assert client.get("/api/scheduling/pools", headers=AUTH).json()["nodes"][0]["restricted"]


# --- kinds of models (model_facts) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kinds"),
    [
        ("Qwen3-30B-A3B-Q4_K_M", ("chat",)),
        ("bge-m3-Q8_0", ("embedding",)),
        ("nomic-embed-text-v1.5", ("embedding",)),
        ("bge-reranker-v2-m3", ("embedding", "rerank")),
        ("whisper-large-v3", ("stt",)),
        ("kokoro-82M", ("tts",)),
        ("Qwen2.5-VL-7B-Instruct-Q4_K_M", ("chat", "vision")),
    ],
)
def test_kinds_are_detected_from_the_name(name: str, kinds: tuple[str, ...]) -> None:
    from topcoat_hub import model_facts

    assert model_facts.detect(name) == kinds


def test_kinds_by_hand_win_and_must_be_known() -> None:
    from topcoat_hub import model_facts

    assert model_facts.kinds_of("odd-name", {"odd-name": ("embedding",)}) == ("embedding",)
    assert model_facts.source_of("odd-name", {"odd-name": ("embedding",)}) == "manual"
    for bad in ({"m": ["chatty"]}, {"m": []}, {"m@x": ["chat"]}):
        with pytest.raises(PolicyError):
            parse_policy({"model_kinds": bad})


def test_the_api_shows_and_sets_kinds(client: TestClient, world) -> None:
    models = client.get("/api/scheduling/pools", headers=AUTH).json()["nodes"][0]["models"]
    assert models[0]["kinds"] == ["chat"] and models[0]["kinds_source"] == "detected"
    r = client.put(
        "/api/scheduling/model-kinds", headers=AUTH, json={"model_kinds": {"big": ["embedding"]}}
    )
    assert r.status_code == 200, r.text
    by_name = {m["name"]: m for m in r.json()["nodes"][0]["models"]}
    assert by_name["big"]["kinds"] == ["embedding"] and by_name["big"]["kinds_source"] == "manual"
    bad = client.put(
        "/api/scheduling/model-kinds", headers=AUTH, json={"model_kinds": {"big": ["x"]}}
    )
    assert bad.status_code == 400


def test_the_fallback_is_the_best_model_the_pool_offers(client: TestClient, world) -> None:
    # nothing is set per application: the best model of the pool answers
    first = client.get("/api/scheduling/pools", headers=AUTH).json()["default_model"]
    assert first in ("big", "small")
    # the pool takes one away: the fallback moves to what is left
    client.put(
        "/api/scheduling/pools", headers=AUTH, json={"pools": {"node-amd": {"available": ["big"]}}}
    )
    assert client.get("/api/scheduling/pools", headers=AUTH).json()["default_model"] == "big"


# --- how many models a node holds at once ------------------------------------------------------


def test_max_loaded_is_validated_and_listed(client: TestClient, world) -> None:
    for bad in ({"node-amd": 0}, {"node-amd": 99}, {"nope": 1}):
        r = client.put("/api/scheduling/max-loaded", headers=AUTH, json={"max_loaded": bad})
        assert r.status_code == 400, bad
    r = client.put("/api/scheduling/max-loaded", headers=AUTH, json={"max_loaded": {"node-amd": 1}})
    assert r.status_code == 200 and r.json()["nodes"][0]["max_loaded"] == 1
    r = client.put("/api/scheduling/max-loaded", headers=AUTH, json={"max_loaded": {}})
    assert r.json()["nodes"][0]["max_loaded"] is None


def test_a_load_unloads_the_idle_model_when_the_node_holds_one(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["big"])
    client.put("/api/scheduling/max-loaded", headers=AUTH, json={"max_loaded": {"node-a": 1}})
    r = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "small", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, r.text
    names = [m["name"] for m in node.loaded]
    assert names == ["small"]  # big was unloaded to make room, never two at once


def test_a_model_the_node_already_unloaded_is_room_enough(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["big"])
    client.put("/api/scheduling/max-loaded", headers=AUTH, json={"max_loaded": {"node-a": 1}})
    node.unload_error = "unload big: not loaded: big"  # the node's idle unload was faster
    r = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "small", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, r.text
    assert [m["name"] for m in node.loaded] == ["small"]


def test_a_refused_unload_says_why(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["big"])
    client.put("/api/scheduling/max-loaded", headers=AUTH, json={"max_loaded": {"node-a": 1}})
    node.unload_error = "unload big: process did not stop"
    r = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "small", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code >= 500 and "process did not stop" in r.text


def test_without_a_limit_two_models_stay_loaded(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["big"])
    r = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "small", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, r.text
    assert sorted(m["name"] for m in node.loaded) == ["big", "small"]
