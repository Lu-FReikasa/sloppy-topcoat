"""A block step whose answer broke off on the node is tried once more on another place (issue 2.18).

Live 2026-11-22: gpt-oss was unloaded on node-desk during the last step of two documentation runs
(B26 and D8 Sheets, 55 and 45 finished paragraphs) and both runs ended as failed."""

from types import SimpleNamespace

import pytest

from topcoat_hub.blocks import pick as picks
from topcoat_hub.blocks import service
from topcoat_hub.blocks.engine import BlockFailure
from topcoat_hub.gateway import CUT, GatewayError
from topcoat_hub.model_resolver import ModelResolver
from topcoat_hub.node_policy import NodePolicy

OK = {"choices": [{"message": {"content": "ok"}}]}


class Gateway:
    def __init__(self, policy: NodePolicy, answers: list[object]) -> None:
        self.policy = policy
        self.resolver = ModelResolver(self)  # type: ignore[arg-type]
        self.answers = answers
        self.sent: list[str] = []

    async def route(self, requested: str) -> SimpleNamespace:
        return SimpleNamespace(candidates=[requested.partition("@")[2]])

    def _has_room(self, node: str) -> bool:
        return True

    async def call_streamed(self, requested: str, path: str, payload, label: str):  # type: ignore[no-untyped-def]
        self.sent.append(requested)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def cut() -> GatewayError:
    return GatewayError(502, "model_error", "the node ended the answer: interrupted", reason=CUT)


HOUSE = NodePolicy(house={"node-desk": "gpt-oss", "node-tower": "coder"})
MSGS = [{"role": "user", "content": "x"}]


def ports(gateway: Gateway) -> service.HubBlockPorts:
    return service.HubBlockPorts(gateway, None, None, None, SimpleNamespace())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_cut_answer_is_tried_once_more_on_the_next_house_node() -> None:
    gw = Gateway(HOUSE, [cut(), OK])
    assert await ports(gw).ask(MSGS, None, 100, 0.0) == ("ok", "coder")
    assert gw.sent == ["gpt-oss@node-desk", "coder@node-tower"]


@pytest.mark.asyncio
async def test_a_second_cut_ends_the_step_and_says_both() -> None:
    gw = Gateway(HOUSE, [cut(), cut()])
    with pytest.raises(BlockFailure, match="once more on coder@node-tower"):
        await ports(gw).ask(MSGS, None, 100, 0.0)
    assert len(gw.sent) == 2


@pytest.mark.asyncio
async def test_other_errors_are_not_repeated() -> None:
    gw = Gateway(HOUSE, [GatewayError(503, "context_full", "the context is full")])
    with pytest.raises(BlockFailure):
        await ports(gw).ask(MSGS, None, 100, 0.0)
    assert gw.sent == ["gpt-oss@node-desk"]


@pytest.mark.asyncio
async def test_a_pick_keeps_its_model_and_only_changes_the_node(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gw = Gateway(NodePolicy(), [cut(), OK])
    p = ports(gw)

    async def candidates() -> list[picks.Candidate]:
        return [
            picks.Candidate("gpt-oss", "node-desk"),
            picks.Candidate("coder", "node-tower"),
            picks.Candidate("gpt-oss", "node-laptop"),
        ]

    monkeypatch.setattr(p, "candidates", candidates)
    picked = SimpleNamespace(
        requested="gpt-oss@node-desk", model="gpt-oss", node="node-desk", exclusive=False
    )
    assert await p.ask(MSGS, picked, 100, 0.0) == ("ok", "gpt-oss")  # type: ignore[arg-type]
    assert gw.sent == ["gpt-oss@node-desk", "gpt-oss@node-laptop"]
    # no other node with that model: the step ends after the first cut
    gw2 = Gateway(NodePolicy(), [cut()])
    p2 = ports(gw2)

    async def alone() -> list[picks.Candidate]:
        return [picks.Candidate("gpt-oss", "node-desk")]

    monkeypatch.setattr(p2, "candidates", alone)
    with pytest.raises(BlockFailure):
        await p2.ask(MSGS, picked, 100, 0.0)  # type: ignore[arg-type]
    assert gw2.sent == ["gpt-oss@node-desk"]
