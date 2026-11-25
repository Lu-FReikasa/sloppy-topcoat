"""A Gründlich answer is held back until its check has passed (api/chat.py, PLAN 2a): the parts that
need no database. The stream and the database path are tested with the chat tests."""

from types import SimpleNamespace

import pytest

from topcoat_hub.api.chat import Held, _held_for, _revise
from topcoat_hub.gateway import GatewayError


class FakeGateway:
    def __init__(self, reply=None, error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[dict] = []

    async def call(self, model, path, payload, min_context=0, label=""):
        self.calls.append({"model": model, "payload": payload, "label": label})
        if self.error is not None:
            raise self.error
        return {"choices": [{"message": {"content": self.reply}}]}


def state_with(gateway):
    return SimpleNamespace(gateway=gateway, settings=SimpleNamespace(agent_min_context=0))


def turn(**over):
    base = dict(
        second_look=True,
        verify_model="qwen-4b",
        quality="thorough",
        model="gemma",
        last_user_text="what is up?",
    )
    return SimpleNamespace(**(base | over))


def prompt():
    return SimpleNamespace(messages=[{"role": "system", "content": "sys"}])


def test_only_the_text_is_hidden_until_release():
    held = Held([])
    assert held.hides('event: delta\ndata: {"text":"a"}\n\n')
    assert not held.hides('event: status\ndata: {"state":"x"}\n\n')
    held.released = True
    assert not held.hides('event: delta\ndata: {"text":"a"}\n\n')


SETTINGS = SimpleNamespace(effort_thorough_revisions=2, agent_min_context=0)


def test_no_revisions_in_the_settings_means_no_held_answer():
    no_revisions = SimpleNamespace(effort_thorough_revisions=0)
    assert _held_for(turn(), prompt(), None, no_revisions) is None


def test_gruendlich_holds_the_answer_and_the_other_stages_do_not():
    assert isinstance(_held_for(turn(), prompt(), None, SETTINGS), Held)
    assert _held_for(turn(quality="balanced"), prompt(), None, SETTINGS) is None
    assert _held_for(turn(quality="fast"), prompt(), None, SETTINGS) is None


def test_no_held_answer_without_a_second_look_or_with_a_tool_call_path():
    assert _held_for(turn(second_look=False), prompt(), None, SETTINGS) is None
    assert _held_for(turn(verify_model=None), prompt(), None, SETTINGS) is None
    assert _held_for(turn(), prompt(), lambda focus: None, SETTINGS) is None


@pytest.mark.asyncio
async def test_a_revision_is_asked_from_the_prompt_with_the_draft_and_the_findings():
    gw = FakeGateway(reply="  the corrected answer  ")
    held = Held([{"role": "system", "content": "sys"}])
    text = await _revise(state_with(gw), turn(), held, "draft", ["the port is invented"])
    assert text == "the corrected answer"
    messages = gw.calls[0]["payload"]["messages"]
    assert messages[0] == {"role": "system", "content": "sys"}
    assert messages[1] == {"role": "assistant", "content": "draft"}
    assert "the port is invented" in messages[2]["content"]
    assert gw.calls[0]["payload"]["stream"] is False
    assert gw.calls[0]["model"] == "gemma"


@pytest.mark.asyncio
async def test_a_revision_that_fails_keeps_the_draft():
    held = Held([])
    gw = FakeGateway(error=GatewayError(503, "busy", "node busy"))
    assert await _revise(state_with(gw), turn(), held, "draft", ["x"]) is None


@pytest.mark.asyncio
async def test_an_empty_revision_is_no_revision():
    held = Held([])
    assert await _revise(state_with(FakeGateway(reply="   ")), turn(), held, "d", ["x"]) is None
