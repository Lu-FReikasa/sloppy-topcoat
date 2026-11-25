"""Probes that prove what a model can do (PRG-00 R1-Rest): a tool call and an embedding. The
probe runs through the gateway's ``call``, so these tests use a fake that answers like a node."""

import pytest

from topcoat_hub.gateway import GatewayError
from topcoat_hub.model_facts import capabilities, kinds_of, proven_kinds, source_of
from topcoat_hub.model_probes import embedding_passed, run_probes, tools_passed

TOOL_ANSWER = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "1", "type": "function", "function": {"name": "x"}}],
            }
        }
    ]
}
PLAIN_ANSWER = {"choices": [{"message": {"role": "assistant", "content": "Hello."}}]}
VECTOR_ANSWER = {"data": [{"embedding": [0.1, -0.2, 0.3]}]}


def test_a_tool_call_proves_tools_and_a_plain_answer_does_not():
    assert tools_passed(TOOL_ANSWER)
    assert not tools_passed(PLAIN_ANSWER)
    assert not tools_passed({"choices": []})


def test_a_vector_proves_embedding_and_text_does_not():
    assert embedding_passed(VECTOR_ANSWER)
    assert not embedding_passed({"data": [{"embedding": []}]})
    assert not embedding_passed({"data": [{"embedding": ["a"]}]})
    assert not embedding_passed(PLAIN_ANSWER)


async def test_each_probe_is_recorded_on_its_own_result():
    paths = []

    async def call(pinned, path, payload, label=""):
        paths.append((pinned, path))
        if path == "/v1/chat/completions":
            return TOOL_ANSWER
        raise GatewayError(502, "model_error", "no embeddings here")

    results = await run_probes(call, "m@node-a", node="node-a")

    assert results["tools"]["ok"] is True
    assert results["embedding"]["ok"] is False
    assert results["tools"]["node"] == "node-a"
    assert ("m@node-a", "/v1/embeddings") in paths


async def test_a_gateway_error_that_ends_the_run_is_raised():
    async def call(pinned, path, payload, label=""):
        raise GatewayError(409, "node_not_found", "gone")

    with pytest.raises(GatewayError):
        await run_probes(call, "m@node-a", node="node-a")


def test_an_embedding_proof_is_a_kind_and_a_tool_proof_is_only_a_capability():
    proofs = {
        "m1": {"embedding": {"ok": True, "node": "n", "at": "t"}},
        "m2": {"tools": {"ok": True, "node": "n", "at": "t"}},
        "m3": {"embedding": {"ok": False, "node": "n", "at": "t"}},
    }
    assert proven_kinds(proofs) == {"m1": ("embedding",)}
    assert capabilities(proofs, "m2") == {"tools": True, "embedding": None}
    assert capabilities(proofs, "m3") == {"tools": None, "embedding": False}
    assert capabilities(proofs, "unknown") == {"tools": None, "embedding": None}


def test_the_admin_setting_beats_a_proof_and_a_proof_beats_the_name():
    manual = {"m": ("chat",)}
    proven = {"m": ("embedding",), "other-bge-model": ("embedding",)}
    assert kinds_of("m", manual, proven) == ("chat",)
    assert source_of("m", manual, proven) == "manual"
    assert kinds_of("m", None, proven) == ("embedding",)
    assert source_of("m", None, proven) == "proven"
    # no proof and no admin setting: the name decides, as before
    assert kinds_of("plain-chat-model", None, proven) == ("chat",)
    assert source_of("plain-chat-model", None, proven) == "detected"
