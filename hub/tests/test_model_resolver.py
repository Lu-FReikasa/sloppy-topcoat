"""One place decides which node and model do a piece of work (consolidation step 3).

The first tests are the table of ``test_model_selection_baseline.py`` asked of the resolver (the
same table held before the roles existed, with the older routes and default models). The rest is
what roles add."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import FakeNode, eventually
from tests.policy_helpers import table_of
from tests.test_gateway import _bench
from tests.test_model_selection_baseline import POLICY, _is_in_state
from tests.test_model_selection_baseline import world as world  # the fixture, shared
from topcoat_hub import applications, memory_tags
from topcoat_hub.chat_engine.answer import PROGRAM_NAMES
from topcoat_hub.model_resolver import Choice, ModelFacts, best_model, is_embedding_name

# --- auto: the model that is large enough and still fast enough ------------------------------

# the figures of the production hub's GPU node: a 26B mixture-of-experts model is fast, a 30B one
# that does not fit the card crawls
GPU_NODE = [
    ModelFacts("gemma-4-26B-A4B-it-QAT-Q4_0", 15000, 217.0, False),
    ModelFacts("gemma-4-12B-it-QAT-Q4_0", 7000, 335.0, True),
    ModelFacts("Qwen3-Coder-30B-A3B-Instruct-IQ4_NL", 17000, 12.1, True),
    ModelFacts("Llama-3.2-3B-Instruct-Q4_K_M", 2000, 400.0, False),
    ModelFacts("bge-m3-q8_0", 600, None, True),
]


def test_auto_takes_the_largest_model_that_is_fast_enough() -> None:
    assert best_model(GPU_NODE, "answer").name == "gemma-4-26B-A4B-it-QAT-Q4_0"
    # a check with tools needs 15 tokens a second: the slow coder model still does not qualify
    assert best_model(GPU_NODE, "agent").name == "gemma-4-26B-A4B-it-QAT-Q4_0"
    # nobody waits for background work: the biggest model that is not hopeless
    assert best_model(GPU_NODE, "background").name == "Qwen3-Coder-30B-A3B-Instruct-IQ4_NL"


def test_auto_without_a_fast_model_keeps_what_the_node_runs() -> None:
    slow = [
        ModelFacts("big-a", 20000, 4.0, False),
        ModelFacts("big-b", 18000, 6.0, True),
        ModelFacts("tiny", 500, None, False),
    ]
    assert best_model(slow, "answer").name == "big-b"  # loaded wins over unloaded
    unloaded = [ModelFacts("big-a", 20000, 4.0, False), ModelFacts("big-b", 18000, 6.0, False)]
    assert best_model(unloaded, "answer").name == "big-b"  # the fastest of the measured ones


def test_auto_without_any_measurement_takes_the_smallest_and_never_an_embedder() -> None:
    unknown = [ModelFacts("large", 9000), ModelFacts("small", 1000), ModelFacts("bge-m3", 100)]
    assert best_model(unknown, "answer").name == "small"
    assert best_model([ModelFacts("bge-m3-q8_0", 600)], "answer") is None
    assert is_embedding_name("bge-m3-FP16") and not is_embedding_name("gemma-4-12B")


# --- the older routes and defaults read as roles ----------------------------------------------


def resolve(client: TestClient, role: str, **kw) -> Choice:
    return client.portal.call(lambda: client.app.state.gateway.resolver.resolve(role, **kw))


def test_the_resolver_gives_the_baseline_table(client: TestClient, world) -> None:
    for role, node, model in (
        ("answer", "node-amd", "big"),
        ("agent", "node-amd", "big"),
        ("background", "node-laptop", "small"),
        ("check", "node-laptop", "small"),
    ):
        got = resolve(client, role)
        assert (got.node, got.model, got.fallback_from) == (node, model, None), role
        assert got.requested.startswith(f"{model}@{node}")  # (the row may name the rest, too)


def test_the_gpu_node_busy_with_a_game_moves_the_answer_and_says_why(
    client: TestClient, world: dict[str, FakeNode]
) -> None:
    amd = world["node-amd"]
    amd.state = "busy_gaming"
    amd.heartbeat()
    eventually(lambda: _is_in_state(client, "node-amd", "busy_gaming"))
    got = resolve(client, "answer")
    assert (got.node, got.model) == ("node-laptop", "big")
    assert (got.fallback_from, got.fallback_state) == ("node-amd", "busy_gaming")
    assert "can take it now" in got.reason and "the row chat of the table" in got.reason


def test_a_person_s_pick_wins_over_every_list(client: TestClient, world) -> None:
    got = resolve(client, "answer", picked="small@node-laptop")
    assert (got.node, got.model, got.reason) == ("node-laptop", "small", "chosen by the person")
    assert resolve(client, "answer", picked="big").node is None  # a model only: any node


def put_rows(client: TestClient, **rows) -> None:
    """The baseline table (``POLICY`` turned into rows), with the given rows on top of it."""
    r = client.put(
        "/api/scheduling/applications",
        headers=AUTH,
        json={"rows": {**table_of(POLICY), **rows}},
    )
    assert r.status_code == 200, r.text


def test_nothing_ordered_is_the_hub_default_and_the_agent_asks_for_context(
    client: TestClient, world
) -> None:
    cleared = client.put("/api/scheduling/applications", headers=AUTH, json={"rows": {}})
    assert cleared.status_code == 200
    got = resolve(client, "answer")
    # no global default is set: the best model the pools offer (the largest of those loaded here)
    assert (got.model, got.node) == ("big", None) and "default" in got.reason
    assert resolve(client, "answer", fallback="big").model == "big"
    assert resolve(client, "agent").min_context == client.app.state.settings.agent_min_context
    assert resolve(client, "answer").min_context == 0


def test_when_no_node_of_the_row_can_take_it_the_caller_chooses_what_comes_back(
    client: TestClient, world
) -> None:
    put_rows(client, chat={"models": ["big"], "nodes": ["node-amd"]})
    amd = world["node-amd"]
    amd.state = "busy_gaming"
    amd.heartbeat()
    eventually(lambda: _is_in_state(client, "node-amd", "busy_gaming"))
    first = resolve(client, "answer")
    assert (first.node, first.model) == ("node-amd", "big")  # the first entry of the row
    assert "can take it now" in first.reason
    default = resolve(client, "answer", fallback="small", if_none="default")
    assert (default.node, default.model) == (None, "small")


# --- rows, and rows of an application ---------------------------------------------------------


def test_a_row_that_is_stored_again_replaces_the_one_before(client: TestClient, world) -> None:
    put_rows(client, chat={"models": ["small", "big"], "nodes": ["node-laptop", "node-amd"]})
    got = resolve(client, "answer")
    assert (got.node, got.model) == ("node-laptop", "small")
    assert "the row chat of the table" in got.reason
    assert resolve(client, "agent").node == "node-laptop"  # an agent run uses the row too
    view = client.get("/api/scheduling/applications", headers=AUTH).json()
    assert view["rows"]["chat"]["models"] == ["small", "big"]
    assert (view["preview"]["chat"]["node"], view["preview"]["chat"]["model"]) == (
        "node-laptop",
        "small",
    )
    assert view["keys"] == [
        "chat",
        "research",
        "dev",
        "admin",
        "project",
        "check",
        "background",
        "api",
    ]


def test_an_application_has_a_row_of_its_own(client: TestClient, world) -> None:
    put_rows(
        client,
        chat={"models": ["small"], "nodes": ["node-laptop"]},
        dev={"models": ["big"], "nodes": ["node-amd"]},
    )
    assert resolve(client, "answer", application="dev").node == "node-amd"
    # an application whose row was not touched keeps the baseline row
    assert resolve(client, "answer", application="research").node == "node-amd"
    assert resolve(client, "answer").node == "node-laptop"  # work of no application is the chat's


def test_auto_is_decided_by_the_speeds_measured_on_the_node(client: TestClient, world) -> None:
    _bench(client, "node-amd", "small", 80.0)
    _bench(client, "node-amd", "big", 9.0)
    put_rows(
        client,
        chat={"nodes": ["node-amd"]},  # no model named: the best one by speed
        background={"nodes": ["node-amd"]},
    )
    # an answer must be fast: only "small" is; background work may take the bigger model
    assert resolve(client, "answer").model == "small"
    assert resolve(client, "background").model == "big"


def test_a_row_can_be_changed_and_taken_away_again(client: TestClient, world) -> None:
    put_rows(client, check={"models": ["big"], "nodes": ["node-amd"]})
    assert resolve(client, "check").node == "node-amd"
    put_rows(client)  # the baseline rows again
    assert resolve(client, "check").node == "node-laptop"
    client.put("/api/scheduling/applications", headers=AUTH, json={"rows": {}})
    assert resolve(client, "check").node is None  # no row: any node that has the model


# --- the applications come from one file ------------------------------------------------------


def test_the_applications_are_one_list() -> None:
    keys = applications.keys()
    assert keys == ("chat", "research", "dev", "admin", "project")
    assert keys == memory_tags.PROGRAMS
    assert set(PROGRAM_NAMES["de"]) == set(keys) == set(PROGRAM_NAMES["en"])
    assert PROGRAM_NAMES["de"]["dev"] == "Entwicklung"
    assert PROGRAM_NAMES["en"]["dev"] == "Development"
    assert applications.get("nowhere") is None and applications.get("dev").hue == 25


def test_a_row_that_names_several_nodes_leaves_the_node_to_the_queue(
    client: TestClient, world
) -> None:
    put_rows(client, chat={"models": ["big"], "nodes": ["node-amd", "node-laptop"]})
    got = resolve(client, "answer")
    assert (got.node, got.model) == ("node-amd", "big")  # where it would start
    assert got.nodes == ("node-amd", "node-laptop")
    assert got.requested == "big@node-amd,node-laptop"  # either may take it
    put_rows(client, chat={"models": ["big"], "nodes": ["node-amd"]})
    assert resolve(client, "answer").requested == "big@node-amd"  # one machine: pinned as before


def test_the_switch_neu_prefers_the_best_rated_loaded_answer_model(
    client: TestClient, world
) -> None:
    """P2b at the resolver: with the same world, "alt" keeps the old first choice (node-amd with its
    big model); "neu" prefers the model with the better answer rating that is loaded (small on
    node-laptop), and the other pairs stay behind it."""
    from tests.test_model_selection_baseline import chat_choice

    gateway = client.app.state.gateway
    assert chat_choice(client) == ("node-amd", "big", None)
    gateway.answer_quality = {"big": 0.7, "small": 0.9}
    try:
        gateway.choice_mode = "neu"
        assert chat_choice(client) == ("node-laptop", "small", None)
        gateway.answer_quality = {}  # nothing rated: the old order again
        assert chat_choice(client) == ("node-amd", "big", None)
    finally:
        gateway.choice_mode = "alt"
        gateway.answer_quality = {}
