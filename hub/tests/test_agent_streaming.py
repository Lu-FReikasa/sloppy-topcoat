"""An agent run is not cut off by the clock: the model's answer is streamed, every piece shows
that it is alive, and the run ends when the node is gone or stands still - not when an answer
takes long."""

import asyncio
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_agent import finish, start, tool, use, vm, wait_for  # noqa: F401
from topcoat_hub.config import Settings
from topcoat_hub.gateway import Gateway, GatewayError, StreamAssembler, Upstream


def event(obj: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"


def delta(**d: Any) -> bytes:
    return event({"choices": [{"index": 0, "delta": d, "finish_reason": None}]})


PROGRESS = event({"choices": [], "prompt_progress": {"total": 100, "processed": 50}})
END = event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
DONE = b"data: [DONE]\n\n"


def test_text_pieces_become_one_message_and_progress_events_are_ignored() -> None:
    a = StreamAssembler()
    for piece in (PROGRESS, delta(content="Hel"), PROGRESS, delta(content="lo"), END):
        a.feed(piece)
    a.feed(event({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}))
    a.feed(DONE)
    out = a.result()
    assert out["choices"][0]["message"]["content"] == "Hello"
    assert out["choices"][0]["finish_reason"] == "stop"
    assert out["usage"] == {"prompt_tokens": 7, "completion_tokens": 2}
    assert a.done and not a.error


def test_a_tool_call_arrives_in_pieces_and_is_put_together_by_index() -> None:
    a = StreamAssembler()
    a.feed(
        delta(
            tool_calls=[
                {"index": 0, "id": "c1", "function": {"name": "docker.", "arguments": ""}},
                {"index": 1, "id": "c2", "function": {"name": "disk.df", "arguments": "{}"}},
            ]
        )
    )
    a.feed(delta(tool_calls=[{"index": 0, "function": {"name": "ps", "arguments": '{"all":'}}]))
    a.feed(delta(tool_calls=[{"index": 0, "function": {"arguments": " true}"}}]))
    a.feed(event({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}))
    calls = a.result()["choices"][0]["message"]["tool_calls"]
    assert [c["id"] for c in calls] == ["c1", "c2"]
    assert calls[0]["function"] == {"name": "docker.ps", "arguments": '{"all": true}'}
    assert calls[1]["function"]["name"] == "disk.df"


def test_a_piece_split_in_the_middle_of_a_character_is_read_whole() -> None:
    raw = delta(content="Grüße")
    a = StreamAssembler()
    cut = raw.index("ü".encode()) + 1  # inside the two bytes of "ü"
    a.feed(raw[:cut])
    a.feed(raw[cut:])
    assert a.result()["choices"][0]["message"]["content"] == "Grüße"


def test_the_error_event_of_the_model_is_kept() -> None:
    a = StreamAssembler()
    a.feed(event({"error": {"message": "slot busy"}}))
    assert a.error == "slot busy"


class _Pieces(Gateway):
    """A gateway whose node answers with the given pieces (bytes), then ends as told."""

    def __init__(self, pieces: list[bytes], *, status: int = 200, stalled: bool = False) -> None:
        self.settings = Settings()
        self.pieces, self.status, self.stall = pieces, status, stalled
        self.payload: dict[str, Any] = {}

    async def forward(self, requested, method, path, payload, accept="", min_context=0):  # type: ignore[override]
        self.payload, self.accept = payload, accept
        pieces = self.pieces

        async def body():
            for p in pieces:
                await asyncio.sleep(0)
                yield p

        up = Upstream(
            status=self.status,
            content_type="text/event-stream",
            body=body(),
            node="node-a",
            close=lambda: None,
        )
        if self.stall:
            up.stalled.append(True)
        return up


def run(coro):
    return asyncio.run(coro)


def test_call_streamed_asks_for_a_stream_with_progress_and_returns_the_plain_shape() -> None:
    gw = _Pieces([PROGRESS, delta(content="ok"), END, DONE])
    out = run(gw.call_streamed("m", "/v1/chat/completions", {"messages": [], "stream": False}))
    assert out["choices"][0]["message"]["content"] == "ok"
    assert gw.payload["stream"] is True and gw.payload["return_progress"] is True
    assert gw.accept == "text/event-stream"


def test_a_node_that_stands_still_is_a_timeout_not_a_short_answer() -> None:
    gw = _Pieces([delta(content="half")], stalled=True)
    with pytest.raises(GatewayError) as exc:
        run(gw.call_streamed("m", "/v1/chat/completions", {}))
    assert exc.value.status == 504


def test_an_answer_that_just_ends_is_not_taken_for_a_complete_one() -> None:
    gw = _Pieces([delta(content="half")])
    with pytest.raises(GatewayError) as exc:
        run(gw.call_streamed("m", "/v1/chat/completions", {}))
    assert exc.value.status == 502


def test_a_refusal_keeps_its_status_text_for_the_fallbacks() -> None:
    gw = _Pieces([b'{"error":{"message":"bad"}}'], status=400)
    with pytest.raises(GatewayError) as exc:
        run(gw.call_streamed("m", "/v1/chat/completions", {}))
    assert "answered 400" in exc.value.message


def test_no_clock_on_a_run_by_default() -> None:
    from topcoat_hub import chat_tools, quality
    from topcoat_hub.agent_loop import Limits

    s = Settings()
    assert Limits.of(s).model_timeout_seconds == 0 and s.agent_stream
    for lim in (
        chat_tools.CHECK_LIMITS,
        chat_tools.SLOW_CHECK_LIMITS,
        quality.THOROUGH_LIMITS,
        quality.WORKSHOP_LIMITS,
    ):
        assert lim.model_timeout_seconds == 0


def test_the_answer_after_the_budget_is_not_cut_short_by_the_budget(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    def slow(_messages: Any) -> str:
        time.sleep(0.4)  # the wrap-up answer takes far longer than the budget left
        return finish("late but whole", [])

    use(client, slow, time_budget_seconds=0)  # the budget is over before the first step
    t = wait_for(client, start(client, "what is up?").json()["id"], "done")
    assert t["stop_reason"] == "time_budget" and t["summary"] == "late but whole", t


def test_the_reasoning_is_kept_next_to_the_answer() -> None:
    """gpt-oss sometimes leaves its answer only in reasoning_content (live 2026-11-18)."""
    a = StreamAssembler()
    for piece in (delta(reasoning_content="think "), delta(reasoning_content='["x"]'), END):
        a.feed(piece)
    message = a.result()["choices"][0]["message"]
    assert message["content"] == "" and message["reasoning_content"] == 'think ["x"]'
