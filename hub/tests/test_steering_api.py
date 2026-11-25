"""The person steers a running check (E7-01, D56): a note the check reads before its next turn, a
pause that does not use up its time, and the rule that only the owner of a check can do it."""

import json
import threading
import time
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import eventually
from tests.test_agent import ScriptedModel, finish, use
from tests.test_chat import new_conv
from tests.test_chat_runs import conversation, drive, events_of, message_id_of
from tests.test_chat_tools import world  # noqa: F401  (a fixture)
from tests.users import make_user

NOTE = "look at the memory first"


class GatedModel(ScriptedModel):
    """A scripted model that holds one call until the test lets it go, so the test can act while
    the check is between two turns."""

    gate_call = 2

    def __init__(self, *replies: Any) -> None:
        super().__init__(*replies)
        self.entered = threading.Event()

    async def complete(self, messages: list[dict[str, str]], model: str | None, max_tokens: int):
        import asyncio

        if len(self.calls) + 1 == self.gate_call:
            self.entered.set()
            while not self.released.is_set():  # noqa: ASYNC110 - set from the test's thread
                await asyncio.sleep(0.01)
        return await super().complete(messages, model, max_tokens)


def look(capability: str, **input: Any) -> str:
    return json.dumps(
        {
            "action": "tool",
            "node": "vm-docker",
            "capability": capability,
            "input": input,
            "reason": f"look with {capability}",
        }
    )


def gated(client: TestClient, *replies: Any) -> GatedModel:
    scripted = use(client, *replies)
    scripted.__class__ = GatedModel
    scripted.entered = threading.Event()  # type: ignore[attr-defined]
    return scripted  # type: ignore[return-value]


def start_check(client: TestClient, conv: str) -> int:
    """Ask a question that runs a check and leave after the first chunk; the run goes on."""
    chunks = client.portal.call(
        lambda: drive(
            client.app,
            f"/api/conversations/{conv}/messages",
            {"content": "Was ist mit web los?", "tools": "on"},
            leave_after=1,
        )
    )
    return message_id_of(chunks)


def test_a_note_and_a_pause_reach_the_check_between_two_turns(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    model = gated(
        client,
        look("docker.logs", container="web", lines=50),
        look("system.info"),
        finish("web ran out of memory."),
    )
    conv = new_conv(client)
    message_id = start_check(client, conv)
    run = client.app.state.runs.live(message_id)
    eventually(lambda: _ready(run, model), timeout=15)  # between turn one and two

    # the person writes a note and pauses the check
    r = client.post(
        f"/api/chat/messages/{message_id}/steer", headers=AUTH, json={"text": f"  {NOTE}\n"}
    )
    assert r.status_code == 200 and r.json() == {"queued": True, "text": NOTE}
    assert client.post(
        f"/api/chat/messages/{message_id}/pause", headers=AUTH, json={"paused": True}
    ).json() == {"paused": True}

    model.released.set()  # turn two answers; the check then stops before turn three
    eventually(lambda: _calls(model, 2), timeout=10)
    time.sleep(0.5)
    assert len(model.calls) == 2  # paused: no third turn yet

    assert (
        client.post(
            f"/api/chat/messages/{message_id}/pause", headers=AUTH, json={"paused": False}
        ).status_code
        == 200
    )
    eventually(lambda: _calls(model, 3), timeout=10)

    # the third turn has the note, the earlier ones do not
    seen = [json.dumps(call) for call in model.calls]
    assert NOTE not in seen[0] and NOTE not in seen[1] and NOTE in seen[2]
    assert "Note from the person who asked" in seen[2]

    eventually(lambda: _finished(client, conv, message_id), timeout=20)
    answer = _closing_bubble(client, conv, message_id)  # it carries the chain
    assert answer is not None
    steer = [i for i in answer["activity"] if i["kind"] == "steer"]
    assert [i["detail"] or i["data"].get("event") for i in steer] == [NOTE, "pause", "resume"]
    assert steer[0]["status"] == "info"


def _ready(run: Any, model: GatedModel) -> None:
    assert run is not None and run.agent_task_id is not None and model.entered.is_set()


def _calls(model: GatedModel, count: int) -> None:
    assert len(model.calls) >= count


def _closing_bubble(client: TestClient, conv: str, message_id: int) -> dict[str, Any] | None:
    """The answer that closes the turn. The message the stream names comes first and is complete
    long before the check has ended; the closing bubble comes after it and carries the chain."""
    later = [
        m
        for m in conversation(client, conv)["messages"]
        if m["id"] > message_id and m["role"] == "assistant" and m["kind"] == "say"
    ]
    return later[-1] if later else None


def _finished(client: TestClient, conv: str, message_id: int) -> None:
    answer = _closing_bubble(client, conv, message_id)
    assert answer is not None and answer["status"] == "complete"


def test_only_the_owner_of_a_running_check_can_steer_it(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    model = gated(client, look("system.info"), look("docker.ps"), finish("done."))
    conv = new_conv(client)
    message_id = start_check(client, conv)
    run = client.app.state.runs.live(message_id)
    eventually(lambda: _ready(run, model), timeout=15)
    _, other = make_user(TEST_DB_URL, "bob")
    for path, body in (("steer", {"text": "hi"}), ("pause", {"paused": True})):
        r = client.post(f"/api/chat/messages/{message_id}/{path}", headers=other, json=body)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_running"
    assert (
        client.post(
            f"/api/chat/messages/{message_id}/steer", headers=AUTH, json={"text": " \x00 "}
        ).status_code
        == 422
    )
    model.released.set()
    eventually(lambda: _finished(client, conv, message_id), timeout=20)
    # the check is over: nothing can be steered any more
    r = client.post(f"/api/chat/messages/{message_id}/steer", headers=AUTH, json={"text": "late"})
    assert r.status_code == 409


def test_an_answer_without_a_check_cannot_be_steered(client: TestClient, world) -> None:  # noqa: F811
    from tests.test_chat_runs import leave

    conv = new_conv(client)
    message_id = message_id_of(leave(client, conv, "Hallo"))
    r = client.post(f"/api/chat/messages/{message_id}/steer", headers=AUTH, json={"text": "x"})
    assert r.status_code == 409 and r.json()["detail"]["code"] in ("no_check", "not_running")
    assert events_of([]) == []
