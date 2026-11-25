"""What the header of a GGUF file says about a model (R8): the node reports it in the inventory and
the hub derives the kind from it, below an admin setting and a probe, above the name."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import INVENTORY, eventually, pair
from topcoat_hub.model_facts import from_gguf, kinds_of, source_of

EMBEDDER = {
    "architecture": "bert",
    "context_length": 8192,
    "embedding_length": 1024,
    "pooling_type": 2,
}
CHATTER = {"architecture": "qwen3", "context_length": 40960, "chat_template": True}


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (EMBEDDER, ("embedding",)),
        ({**EMBEDDER, "pooling_type": 1}, ("embedding",)),
        ({"architecture": "bert", "pooling_type": 4}, ("rerank",)),  # a reranker is no embedder
        ({"architecture": "clip"}, ("vision",)),
        (CHATTER, ("chat",)),
        ({"architecture": "llama"}, None),  # nothing decisive: the name decides
        (None, None),
        ({}, None),
    ],
)
def test_the_header_names_the_kind_when_it_says_enough(facts, expected) -> None:
    assert from_gguf(facts) == expected


def test_the_order_is_admin_then_probe_then_header_then_name() -> None:
    name = "mystery-model"  # the name says nothing: a chat model
    assert kinds_of(name) == ("chat",)
    assert kinds_of(name, gguf=EMBEDDER) == ("embedding",)
    assert source_of(name, gguf=EMBEDDER) == "gguf"
    assert kinds_of(name, proven={name: ("chat",)}, gguf=EMBEDDER) == ("chat",)
    assert source_of(name, proven={name: ("chat",)}, gguf=EMBEDDER) == "proven"
    assert kinds_of(name, manual={name: ("rerank",)}, gguf=EMBEDDER) == ("rerank",)
    assert source_of(name, manual={name: ("rerank",)}, gguf=EMBEDDER) == "manual"


def test_a_chat_model_whose_name_says_vision_keeps_both_kinds() -> None:
    assert kinds_of("some-vl-model", gguf=CHATTER) == ("chat", "vision")
    assert kinds_of("plain-model", gguf=CHATTER) == ("chat",)


def test_the_pools_page_takes_the_kind_from_the_files_the_node_reports(
    client: TestClient, connect
) -> None:
    node_id, key = pair(client, "node-a")
    node = connect(node_id, key)
    node.inventory = {
        **INVENTORY,
        "models": [
            {"name": "mystery-model", "size_bytes": 600, "format": "gguf", "gguf": EMBEDDER},
            {"name": "plain-model", "size_bytes": 900, "format": "gguf", "gguf": CHATTER},
            {"name": "no-header", "size_bytes": 900, "format": "gguf"},
        ],
    }
    node.handshake()
    node.serve()
    node.heartbeat()

    def seen() -> dict:
        pools = client.get("/api/scheduling/pools", headers=AUTH).json()
        models = {m["name"]: m for n in pools["nodes"] for m in n["models"]}
        assert {"mystery-model", "plain-model", "no-header"} <= set(models)
        return models

    models = eventually(seen)
    assert (models["mystery-model"]["kinds"], models["mystery-model"]["kinds_source"]) == (
        ["embedding"],
        "gguf",
    )
    assert models["plain-model"]["kinds_source"] == "gguf"
    assert (models["no-header"]["kinds"], models["no-header"]["kinds_source"]) == (
        ["chat"],
        "detected",
    )
