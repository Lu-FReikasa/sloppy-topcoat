"""The pipeline of an answer (chat_engine/pipeline.py, issue #205): a failure that is not a
GatewayError must neither leave the request waiting nor keep the model's stream open."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakenode import eventually, online_node
from tests.test_chat import answer_text, ask, new_conv, use_model
from topcoat_hub.chat_engine import answer as chat_api


def test_a_crash_before_the_model_is_asked_is_a_500_not_a_wait(
    client: TestClient, connect, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("a bug in a step of the pipeline")

    monkeypatch.setattr(chat_api, "_core_text", broken)
    status, _, r = ask(client, new_conv(client), "Hallo?")
    assert status == 500
    assert r.json()["detail"]["code"] == "internal_error"
    assert not node.proxy_requests  # the model was never asked


@pytest.mark.timeout(30)  # a regression waits forever otherwise
def test_a_crash_after_the_model_answered_closes_its_stream(
    client: TestClient, connect, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live until #205: the stream stayed open and held the node's only place, so the next
    message there waited forever."""
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    held_for = chat_api._held_for
    calls: list[int] = []

    def broken_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("a bug between the forward and the answer")
        return held_for(*args, **kwargs)

    monkeypatch.setattr(chat_api, "_held_for", broken_once)
    status, _, r = ask(client, new_conv(client), "Hallo?")
    assert status == 500 and r.json()["detail"]["code"] == "internal_error"
    first = node.proxy_requests[0]["body"]["stream_id"]
    eventually(lambda: first in node.cancels)  # the node was told to stop

    status, events, _ = ask(client, new_conv(client), "Und jetzt?")
    assert status == 200
    assert events[-1][0] == "done" and answer_text(events)
