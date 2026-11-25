"""The chain of house models prefers a node with a free place (2026-11-18)."""

import asyncio
from types import SimpleNamespace

from topcoat_hub import node_chain
from topcoat_hub.gateway import GatewayError


class Gateway:
    """Nodes with a number of free places; an unknown node cannot take anything."""

    def __init__(self, free: dict[str, int]) -> None:
        self.free = free

    async def route(self, requested: str) -> SimpleNamespace:
        node = requested.partition("@")[2]
        if node not in self.free:
            raise GatewayError(503, "model_unavailable", f"{node} is offline")
        return SimpleNamespace(candidates=[SimpleNamespace(name=node)])

    def _has_room(self, cand: SimpleNamespace) -> bool:
        return self.free[cand.name] > 0


CHAIN = [("node-nvidia", "coder"), ("node-amd", "gpt-oss")]


def pick(free: dict[str, int]) -> node_chain.Pick | None:
    return asyncio.run(node_chain.pick(Gateway(free), CHAIN))  # type: ignore[arg-type]


def test_a_full_first_node_hands_the_request_to_a_free_one_of_the_row() -> None:
    assert pick({"node-nvidia": 1, "node-amd": 2}) == node_chain.Pick("node-nvidia", "coder", None, None)
    # node-nvidia' one slot is taken: node-amd with its own model answers, and it is no fallback
    assert pick({"node-nvidia": 0, "node-amd": 2}) == node_chain.Pick("node-amd", "gpt-oss", None, None)
    # every node full: the first one, its queue decides as before
    assert pick({"node-nvidia": 0, "node-amd": 0}) == node_chain.Pick("node-nvidia", "coder", None, None)


def test_a_node_that_cannot_take_it_at_all_is_still_a_fallback() -> None:
    got = pick({"node-amd": 0})
    assert got == node_chain.Pick("node-amd", "gpt-oss", "node-nvidia", "offline")
