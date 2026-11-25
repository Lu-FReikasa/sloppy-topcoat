"""House models, tags and the check of citations without a model (2026-11-14).

Live before: three different "first" models in the table on two nodes that hold one model each, so
the nodes swapped models all day; and a second model's look that was wrong more often than right
because it took notes of the memory for the truth."""

from types import SimpleNamespace

import pytest

from topcoat_hub import model_facts
from topcoat_hub.citation_check import cited, literals, unbacked
from topcoat_hub.model_resolver import ModelResolver
from topcoat_hub.node_policy import AppRow, NodePolicy, PolicyError, parse_policy


def resolver(policy: NodePolicy) -> ModelResolver:
    return ModelResolver(SimpleNamespace(policy=policy))  # type: ignore[arg-type]


HOUSE = {"node-amd": "gemma-q2", "node-nvidia": "gemma-q4", "node-laptop": "qwen-4b"}


def test_the_house_model_answers_and_background_goes_to_the_worker_first() -> None:
    policy = NodePolicy(
        house=HOUSE,
        workers=("node-laptop",),
        applications={
            "project": AppRow(models=("other-model",), nodes=("node-nvidia", "node-amd")),
        },
    )
    r = resolver(policy)
    # the row's nodes in its order, each with its own house model; its model list no longer counts
    assert r.house_entries("answer", "project") == [
        ("node-nvidia", "gemma-q4"),
        ("node-amd", "gemma-q2"),
    ]
    # a row without nodes: every node that is no worker
    assert r.house_entries("answer", "chat") == [
        ("node-amd", "gemma-q2"),
        ("node-nvidia", "gemma-q4"),
    ]
    # nobody waits for background work: the worker first, the others behind it
    assert r.house_entries("background", None)[0] == ("node-laptop", "qwen-4b")
    assert len(r.house_entries("background", None)) == 3
    assert resolver(NodePolicy()).house_entries("answer", "chat") == []  # no house: the old table


def test_the_policy_keeps_house_and_workers() -> None:
    raw = {"house": {"node-amd": "gemma-q2"}, "workers": ["node-laptop"]}
    policy = parse_policy(raw)
    assert policy.house == {"node-amd": "gemma-q2"} and policy.workers == ("node-laptop",)
    assert parse_policy(policy.to_json()) == policy
    with pytest.raises(PolicyError):
        parse_policy({"house": {"node-amd": "a@b"}})
    with pytest.raises(PolicyError):
        parse_policy({"house": ["gemma"]})


def _jsonb(value: object) -> object:
    """What Postgres jsonb gives back: the keys of every object sorted by length, then bytes."""
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: (len(k.encode()), k.encode()))
        return {k: _jsonb(value[k]) for k in keys}
    if isinstance(value, list):
        return [_jsonb(v) for v in value]
    return value


def test_the_house_order_survives_the_database() -> None:
    """Issue 2.27 (live 2026-11-23): node-tower was put before node-desk, and after a restart
    node-desk was first again, because jsonb sorts the keys of the house object (shorter
    first)."""
    policy = parse_policy({"house": {"node-tower": "qwen3-coder", "node-desk": "gpt-oss"}})
    stored = _jsonb(policy.to_json())
    # what the column does to the object
    assert list(stored["house"]) == ["node-desk", "node-tower"]  # type: ignore[index]
    again = parse_policy(stored)
    assert list(again.house) == ["node-tower", "node-desk"]
    assert resolver(again).house_default({"node-tower", "node-desk"}) == "qwen3-coder"
    # an order that names a node without a house model is refused, not ignored
    with pytest.raises(PolicyError):
        parse_policy({"house": {"node-desk": "m"}, "house_order": ["node-tower"]})
    # a policy stored before the order existed still parses, in the order it comes
    assert list(parse_policy({"house": {"node-desk": "m", "node-tower": "n"}}).house) == [
        "node-desk",
        "node-tower",
    ]


def test_saving_the_house_keeps_its_order_unless_one_is_given(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import AUTH
    from topcoat_hub.api import scheduling

    async def known(_db):  # type: ignore[no-untyped-def]
        return {"node-a", "node-b", "node-c"}

    monkeypatch.setattr(scheduling, "_known_nodes", known)

    def order() -> list[str]:
        return list(client.app.state.gateway.policy.house)

    first = client.put(
        "/api/scheduling/house",
        json={"house": {"node-a": "m1", "node-b": "m2"}, "order": ["node-b", "node-a"]},
        headers=AUTH,
    )
    assert first.status_code == 200 and order() == ["node-b", "node-a"]
    # the page sends its nodes in list order and no order: the order stays, a new node follows
    again = client.put(
        "/api/scheduling/house",
        json={"house": {"node-a": "m1", "node-c": "m4", "node-b": "m3"}},
        headers=AUTH,
    )
    assert again.status_code == 200 and order() == ["node-b", "node-a", "node-c"]
    bad = client.put(
        "/api/scheduling/house",
        json={"house": {"node-a": "m1"}, "order": ["node-b"]},
        headers=AUTH,
    )
    assert bad.status_code == 400


def test_tags_come_from_the_name_and_the_header() -> None:
    gemma = model_facts.tags(
        "gemma-4-26B-A4B-it-UD-Q4_K_S",
        {"architecture": "gemma4", "context_length": 262144},
        ["chat"],
    )
    assert gemma == ["chat", "gemma4", "26B", "A4B", "moe", "UD Q4_K_S", "256k ctx"]
    small = model_facts.tags("Qwen3.8-4B-Q4_K_M", {"architecture": "qwen35"}, ["chat"])
    assert "small" in small and "moe" not in small
    assert "moe" in model_facts.tags("openai_gpt-oss-20b-MXFP4", {"architecture": "gpt-oss"})
    assert model_facts.tags("plain") == []


def test_a_citation_is_read_from_the_sentence() -> None:
    assert cited("Das Menü heißt 'Me' [E2].") == ["E2"]
    assert cited("Beide liefen [live E2, E5].") == ["E2", "E5"]
    assert cited("Keine Treffer (E1\u2013E3).") == ["E1", "E2", "E3"]
    assert cited("Ohne Beleg.") == []
    assert literals("Kontext 262.144 Tokens in `config.yml` [E6]") == ["config.yml", "262.144"]


def test_a_sentence_its_result_does_not_carry_is_found() -> None:
    results = {
        "E1": '{"node": "topcoat", "state": "available"}',
        "E2": "context_length: 262144, n_parallel: 1",
    }
    answer = (
        "Das Menü findest du unter 'Settings > Me' [E1]. "
        "Der Kontext ist 262.144 Tokens [E2]. "
        "Die Node ist erreichbar [E1]. "
        "Das steht in E9 [E9]."
    )
    found = unbacked(answer, results)
    assert [f["rule"] for f in found] == ["not_in_result", "unknown_result"]
    assert "Settings > Me" in found[0]["reason"]
    assert all(f["severity"] == "minor" and f["evidence"] is None for f in found)
    assert unbacked("Nichts zitiert, 123 Dinge.", results) == []


def test_the_brief_of_the_chat_model_reaches_the_run() -> None:
    from topcoat_hub.chat_tools import decider_tool, order_prompt
    from topcoat_hub.check_order import Order

    order = Order(
        target="any",
        tools=("project",),
        goal="Die Aufgaben 1.5 bis 1.7 ausformulieren",
        result="diagnosis",
        window="24h",
        brief="1.5: Dev liest die Theme-Dateien. 1.6: Recherche zu Windows XP. 1.7: Dev baut es.",
    )
    text = order_prompt(order, "ja gern", [("user", "ja gern")])
    assert "Auftrag vom Gespräch:\n1.5: Dev liest" in text
    assert text.index("Auftrag vom Gespräch") < text.index("Prüfe nur")
    tool = decider_tool(("Projekte",), targets=("project",), groups=("project",))
    assert "brief" in tool["function"]["parameters"]["properties"]


def test_a_house_needs_known_nodes(client) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import AUTH

    bad = client.put("/api/scheduling/house", json={"house": {"no-such-node": "m"}}, headers=AUTH)
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "invalid_house"
    ok = client.put("/api/scheduling/house", json={"house": {}, "workers": []}, headers=AUTH)
    assert ok.status_code == 200 and "nodes" in ok.json()


def test_a_worker_never_checks_another_models_work() -> None:
    from topcoat_hub.chat_review import pick_reviewer

    models = [
        {"id": "big", "topcoat": {"nodes": [{"node": "node-nvidia", "loaded": True}]}},
        {"id": "tiny", "topcoat": {"nodes": [{"node": "node-laptop", "loaded": True}]}},
    ]
    # without the rule "another loaded model on another node" is the laptop's small model
    assert pick_reviewer(models, "big", "", [], "node-nvidia") == "tiny@node-laptop"
    assert (
        pick_reviewer(models, "big", "", [], "node-nvidia", workers=("node-laptop",))
        == "big@node-nvidia"
    )


def test_the_house_model_is_always_in_its_nodes_pool() -> None:
    policy = NodePolicy(pools={"node-laptop": ("bge",)}, house={"node-laptop": "qwen-4b"})
    assert policy.in_pool("node-laptop", "qwen-4b") and policy.in_pool("node-laptop", "bge")
    assert not policy.in_pool("node-laptop", "other")


def test_work_no_row_steers_takes_the_house_model_not_the_fastest_measured_one() -> None:
    # live: Qwen3-Coder had no speed yet, so a block program kept asking the measured Gemma
    policy = NodePolicy(
        house={"node-nvidia": "coder", "node-amd": "gemma-q2", "node-laptop": "qwen-4b"},
        workers=("node-nvidia", "node-laptop"),
    )
    r = resolver(policy)
    assert r.house_default(["node-nvidia", "node-amd", "node-laptop"]) == "gemma-q2"
    # only workers up: a worker's model still beats no answer
    assert r.house_default(["node-laptop", "node-nvidia"]) == "coder"
    assert r.house_default([]) == ""
    assert resolver(NodePolicy()).house_default(["node-amd"]) == ""  # no house: the old way


def _view(name: str, models: dict[str, bool]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        connected=True,
        models=models,
        sizes_mb={m: 1000.0 for m in models},
        speeds={m: 50.0 for m in models},
        gguf={},
    )


@pytest.mark.asyncio
async def test_block_picks_see_only_house_models_and_no_worker(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from topcoat_hub.blocks import service

    async def no_ratings(_sessionmaker):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr(service, "latest_ratings", no_ratings)
    views = [
        _view("node-nvidia", {"coder": True, "gemma-q4": False}),
        _view("node-laptop", {"qwen-4b": True}),
    ]

    async def node_views():  # type: ignore[no-untyped-def]
        return views

    def ports(policy: NodePolicy) -> "service.HubBlockPorts":
        gateway = SimpleNamespace(policy=policy, proven_kinds={}, node_views=node_views)
        return service.HubBlockPorts(gateway, None, None, None, SimpleNamespace())  # type: ignore[arg-type]

    house = NodePolicy(
        house={"node-nvidia": "coder", "node-laptop": "qwen-4b"}, workers=("node-laptop",)
    )
    assert [(c.node, c.model) for c in await ports(house).candidates()] == [
        ("node-nvidia", "coder")
    ]
    # without house models every pool model is a candidate, as before
    assert len(await ports(NodePolicy()).candidates()) == 3


@pytest.mark.asyncio
async def test_work_no_row_steers_passes_over_a_node_with_a_game() -> None:
    """Live 2026-11-18: every block program failed at once with "node is busy" while node-amd ran a
    game; node-nvidia was free and has a house model too."""
    policy = NodePolicy(house={"node-amd": "gpt-oss", "node-nvidia": "coder"})
    views = [
        SimpleNamespace(name="node-amd", connected=True, state="busy_gaming", models={}),
        SimpleNamespace(name="node-nvidia", connected=True, state="available", models={}),
    ]

    async def node_views():  # type: ignore[no-untyped-def]
        return views

    r = ModelResolver(SimpleNamespace(policy=policy, node_views=node_views))  # type: ignore[arg-type]
    assert await r.default_model() == "coder"
    views[0].state = "available"
    assert await r.default_model() == "gpt-oss"
    for v in views:  # every node busy: the first house model still beats no answer
        v.state = "busy_gaming"
    assert await r.default_model() == "gpt-oss"


@pytest.mark.asyncio
async def test_the_memorys_own_work_goes_to_the_main_nodes_before_the_workers(monkeypatch) -> None:
    """2026-11-21: the worker's small model merged two or three notes a run and kept the
    duplicates; the maintainer gave node-tower to the memory. Summaries stay with the workers."""
    from topcoat_hub import memory as memory_mod

    chain = [("node-laptop", "qwen-4b"), ("node-nvidia", "gemma-q4"), ("node-amd", "gemma-q2")]

    class Resolver:
        async def chain(self, role: str) -> list[tuple[str, str]]:
            return list(chain)

    async def first_free(gateway: object, entries: list[tuple[str, str]]) -> object:
        node, model = entries[0]
        return SimpleNamespace(node=node, model=model)

    monkeypatch.setattr(memory_mod.node_chain, "pick", first_free)
    gateway = SimpleNamespace(
        resolver=Resolver(), policy=NodePolicy(house=HOUSE, workers=("node-laptop",))
    )
    from topcoat_hub.config import Settings

    service = memory_mod.MemoryService(None, gateway, Settings(), None)  # type: ignore[arg-type]
    assert await service.routed_model("care") == "gemma-q4@node-nvidia"
    assert (
        await service.routed_model("chat") == "qwen-4b@node-laptop"
    )  # other work: as the chain says


@pytest.mark.asyncio
async def test_a_step_without_pick_never_goes_to_the_worker() -> None:
    """Issue 2.24 (live 2026-11-23): a block step with no pick sent only the model's name, and the
    gateway gave it to the laptop worker, which holds the same model as node-desk."""
    from topcoat_hub.blocks import service

    policy = NodePolicy(
        house={"node-laptop": "gpt-oss", "node-amd": "gpt-oss", "node-nvidia": "coder"},
        workers=("node-laptop",),
    )
    sent: list[str] = []
    full: set[str] = set()

    class Route:
        def __init__(self, node: str) -> None:
            self.candidates = [node]

    class Gateway:
        def __init__(self) -> None:
            self.policy = policy
            self.resolver = ModelResolver(self)  # type: ignore[arg-type]

        async def route(self, requested: str) -> Route:
            return Route(requested.partition("@")[2])

        def _has_room(self, node: str) -> bool:
            return node not in full

        async def call_streamed(self, requested: str, path: str, payload, label: str):  # type: ignore[no-untyped-def]
            sent.append(requested)
            return {"choices": [{"message": {"content": "ok"}}]}

    ports = service.HubBlockPorts(Gateway(), None, None, None, SimpleNamespace())  # type: ignore[arg-type]
    msgs = [{"role": "user", "content": "x"}]
    assert await ports.ask(msgs, None, 100, 0.0) == ("ok", "gpt-oss")
    assert sent[-1] == "gpt-oss@node-amd"  # the house order, without the worker
    full.add("node-amd")  # its places are taken: the next main node, still not the worker
    assert await ports.ask(msgs, None, 100, 0.0) == ("ok", "coder")
    assert sent[-1] == "coder@node-nvidia"
    assert not any(s.endswith("@node-laptop") for s in sent)
