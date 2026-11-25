"""What the hub measures of a node: generation speed of real answers and speed of real loads."""

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.fakenode import eventually, online_node
from tests.test_chat import use_model
from tests.test_memory import chat_once, setup_node
from topcoat_hub import gateway as gateway_module
from topcoat_hub.models import Node, NodeSpeed

WORDS = 40  # of the visible answer
THOUGHT = 120  # words of thinking before it


def speed_of(client: TestClient, node: str) -> tuple[float | None, float | None]:
    async def read() -> tuple[float | None, float | None]:
        async with client.app.state.sessionmaker() as db:
            row = (await db.execute(select(Node).where(Node.name == node))).scalar_one()
            found = await db.scalar(select(NodeSpeed).where(NodeSpeed.node_id == row.id))
            return (found.tg_tps, found.load_mb_s) if found else (None, None)

    return client.portal.call(read)


def test_the_speed_of_an_answer_with_a_long_thought_is_the_real_one(
    client: TestClient, connect: Any
) -> None:
    """The node counts the thinking in the completion tokens, so the time must start at the first
    token of any kind: measured from the first visible token, a long thought looks fast."""
    node = setup_node(client, connect)
    node.stream_text_fn = lambda req: " ".join(f"w{i}" for i in range(WORDS))
    node.stream_reasoning = " ".join(f"t{i}" for i in range(THOUGHT))
    node.usage_counts_reasoning = True
    node.chunk_delay = 0.02  # about 50 tokens per second, thinking included
    chat_once(client, "Wie schnell bist du gerade?")
    eventually(lambda: _noted(client), timeout=10)
    tps = speed_of(client, "node-a")[0]
    assert tps is not None and 15 < tps < 100, tps  # the old measurement gave about 200


def _noted(client: TestClient) -> None:
    assert speed_of(client, "node-a")[0] is not None


def test_the_speed_of_a_real_load_is_noted(
    client: TestClient, connect: Any, monkeypatch: Any
) -> None:
    """A load teaches the hub how fast the node reads a model, which needs the model's size."""
    monkeypatch.setattr(gateway_module, "MIN_LOAD_SECONDS", 0.0)
    node = online_node(client, connect, "node-b")  # "small" only on disk
    use_model(client, "small")
    node.stream_text_fn = lambda req: "ok"
    chat_once(client, "Hallo, bitte laden.")
    eventually(lambda: _load_noted(client), timeout=10)
    assert (speed_of(client, "node-b")[1] or 0) > 0


def _load_noted(client: TestClient) -> None:
    assert speed_of(client, "node-b")[1] is not None
