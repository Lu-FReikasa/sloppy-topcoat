"""The gate before an answer (brief R1-01-effort-stages, plan 2a): the loop with fakes."""

import pytest

from topcoat_hub.answer_gate import MAX_REVISIONS, gate, revision_note
from topcoat_hub.quality import Verification


def verdict(ok: bool, *issues: str) -> Verification:
    return Verification(ok, list(issues), "reviewer")


def scripted(*answers: str):
    """A producer that gives the answers in turn and records the findings it was given."""
    given: list[list[str] | None] = []
    it = iter(answers)

    async def produce(feedback):
        given.append(feedback)
        return next(it)

    return produce, given


def scripted_checks(*results):
    seen: list[str] = []
    it = iter(results)

    async def check(text):
        seen.append(text)
        return next(it)

    return check, seen


@pytest.mark.asyncio
async def test_a_passing_first_answer_is_shown_at_once():
    produce, given = scripted("first")
    check, seen = scripted_checks(verdict(True))
    result = await gate(produce, check, revisions=2)
    assert (result.text, result.answers, result.checked, result.ok) == ("first", 1, True, True)
    assert given == [None]
    assert seen == ["first"]


@pytest.mark.asyncio
async def test_a_finding_makes_one_revision_and_the_revision_is_shown():
    produce, given = scripted("wrong", "right")
    check, _ = scripted_checks(verdict(False, "the version is invented"), verdict(True))
    result = await gate(produce, check, revisions=2)
    assert (result.text, result.answers, result.ok) == ("right", 2, True)
    assert given == [None, ["the version is invented"]]


@pytest.mark.asyncio
async def test_when_the_revisions_run_out_the_last_answer_is_shown_with_its_findings():
    produce, _ = scripted("a", "b", "c")
    check, _ = scripted_checks(
        verdict(False, "one"), verdict(False, "two"), verdict(False, "three")
    )
    result = await gate(produce, check, revisions=2)
    assert result.answers == 3
    assert result.text == "c"
    assert result.checked is True
    assert result.ok is False
    assert result.issues == ["three"]


@pytest.mark.asyncio
async def test_zero_revisions_checks_once_and_does_not_revise():
    produce, given = scripted("only")
    check, _ = scripted_checks(verdict(False, "doubt"))
    result = await gate(produce, check, revisions=0)
    assert (result.answers, result.ok, result.issues) == (1, False, ["doubt"])
    assert given == [None]


@pytest.mark.asyncio
async def test_a_checker_that_cannot_answer_leaves_the_answer_unchecked():
    produce, given = scripted("plain")
    check, _ = scripted_checks(None)
    result = await gate(produce, check, revisions=2)
    assert (result.text, result.answers, result.checked, result.ok) == ("plain", 1, False, False)
    assert given == [None]


@pytest.mark.asyncio
async def test_the_revisions_are_capped_whatever_the_stage_says():
    answers = [f"a{i}" for i in range(MAX_REVISIONS + 5)]
    produce, _ = scripted(*answers)
    check, _ = scripted_checks(*[verdict(False, "x") for _ in answers])
    result = await gate(produce, check, revisions=99)
    assert result.answers == MAX_REVISIONS + 1


@pytest.mark.asyncio
async def test_a_negative_number_of_revisions_is_refused():
    produce, _ = scripted("a")
    check, _ = scripted_checks(verdict(True))
    with pytest.raises(ValueError):
        await gate(produce, check, revisions=-1)


def test_the_revision_note_names_every_finding_and_the_rule():
    note = revision_note(["the port is 8443", "the node is down"])
    assert "- the port is 8443" in note
    assert "- the node is down" in note
    assert "do not add facts" in note
