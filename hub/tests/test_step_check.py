"""The live check of one tool step (topcoat_hub.step_check) with a fake model port."""

import pytest

from topcoat_hub.step_check import MAX_TOKENS, NOTE_CHARS, model_note, parse_note, rule_notes


class FakePort:
    def __init__(self, reply: str | None = None, error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[tuple[list, str, int]] = []

    async def complete(self, messages, model, max_tokens):
        self.calls.append((messages, model, max_tokens))
        if self.error is not None:
            raise self.error
        return self.reply


def test_a_failed_call_is_a_note_without_a_model():
    assert rule_notes("containers.list", ok=False, code="timeout", output="") == [
        "containers.list failed (timeout)"
    ]


def test_an_empty_output_is_a_note():
    assert rule_notes("logs.read", ok=True, code="", output="  \n") == [
        "logs.read returned nothing"
    ]


def test_a_good_result_gives_no_rule_note():
    assert rule_notes("logs.read", ok=True, code="", output="line 1") == []


@pytest.mark.parametrize("reply", ["NONE", "none.", "  NONE  "])
def test_none_gives_no_note(reply):
    assert parse_note(reply) is None


def test_a_sentence_is_the_note_and_is_cut_to_its_length():
    assert parse_note("The result is\nfrom last week.") == "The result is from last week."
    assert len(parse_note("x" * 500)) == NOTE_CHARS


def test_an_empty_or_non_text_reply_gives_no_note():
    assert parse_note("") is None
    assert parse_note(None) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_model_note_asks_with_the_short_budget_and_the_given_model():
    port = FakePort(reply="NONE")
    note = await model_note(port.complete, "qwen-4b", "is it up?", "node.state", {"id": "a"}, "up")
    assert note is None
    messages, model, max_tokens = port.calls[0]
    assert model == "qwen-4b"
    assert max_tokens == MAX_TOKENS
    assert messages[0]["role"] == "system"
    assert "Tool: node.state" in messages[1]["content"]


@pytest.mark.asyncio
async def test_model_note_returns_the_problem_it_names():
    port = FakePort(reply="The status is from yesterday.")
    note = await model_note(port.complete, "m", "q", "node.state", {}, "old")
    assert note == "The status is from yesterday."


@pytest.mark.asyncio
async def test_a_failing_checker_gives_no_note_and_raises_nothing():
    port = FakePort(error=RuntimeError("node busy"))
    assert await model_note(port.complete, "m", "q", "t", {}, "out") is None


@pytest.mark.asyncio
async def test_the_output_the_model_reads_is_cut():
    port = FakePort(reply="NONE")
    await model_note(port.complete, "m", "q", "t", {}, "y" * 5000)
    assert len(port.calls[0][0][1]["content"]) < 1600


def test_only_gruendlich_gets_the_step_check_model():
    from topcoat_hub import quality

    assert quality.check_limits("thorough", False, step_check_model="m").step_check_model == "m"
    assert quality.check_limits("balanced", False, step_check_model="m").step_check_model == ""
    assert quality.check_limits("fast", False, step_check_model="m").step_check_model == ""
    assert quality.check_limits("thorough", True, step_check_model="m").step_check_model == "m"


def test_a_reply_that_echoes_the_result_as_json_or_markup_is_no_note() -> None:
    # a small model that copies the tool result back instead of judging it (seen live, 2026-11-04)
    for echo in (
        '{ "name":"Paperless Scan","status":"down" }',
        '[{"error": "x"}]',
        '```json\n{"summary": "alles gesund"}\n```',
        '"The containers are healthy"',
        "<result>ok</result>",
    ):
        assert parse_note(echo) is None, echo
    assert parse_note("Der Monitor meldet den Dienst als down.") == (
        "Der Monitor meldet den Dienst als down."
    )
