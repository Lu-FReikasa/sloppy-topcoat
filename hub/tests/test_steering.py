"""Steering a running check: notes, pause and resume (E7-01, D56). Plain objects, no database."""

import asyncio
import uuid

import pytest

from topcoat_hub.steering import (
    MAX_NOTE_CHARS,
    MAX_NOTES_PER_TASK,
    SteeringBoard,
    SteeringError,
    clean,
)

TASK = uuid.UUID("00000000-0000-4000-8000-000000000042")


def test_a_note_is_cleaned_before_the_model_reads_it() -> None:
    assert clean("  look \x00at\x07 the   logs\n first ") == "look at the logs first"
    assert len(clean("x" * 2000)) == MAX_NOTE_CHARS


def test_notes_are_read_once_and_in_order() -> None:
    board = SteeringBoard()
    board.push(TASK, "first")
    board.push(TASK, "second")
    assert board.take(TASK) == ["first", "second"]
    assert board.take(TASK) == []  # not kept after reading


def test_an_empty_note_and_a_flood_of_notes_are_refused() -> None:
    board = SteeringBoard()
    with pytest.raises(SteeringError) as empty:
        board.push(TASK, " \x00 ")
    assert empty.value.code == "empty"
    for i in range(MAX_NOTES_PER_TASK):
        board.push(TASK, f"note {i}")
    with pytest.raises(SteeringError) as many:
        board.push(TASK, "one more")
    assert many.value.code == "too_many"


def test_notes_of_one_task_are_not_given_to_another() -> None:
    board = SteeringBoard()
    board.push(TASK, "for the first")
    assert board.take(uuid.uuid4()) == []
    assert board.take(TASK) == ["for the first"]


def test_a_task_that_is_not_paused_does_not_wait() -> None:
    board = SteeringBoard()
    assert asyncio.run(board.wait_while_paused(TASK)) == 0.0
    assert not board.is_paused(TASK)


def test_a_paused_task_waits_until_it_is_resumed_and_says_for_how_long() -> None:
    async def scenario() -> float:
        board = SteeringBoard()
        board.pause(TASK)
        assert board.is_paused(TASK)
        waiter = asyncio.create_task(board.wait_while_paused(TASK, limit=5.0))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        board.resume(TASK)
        waited = await waiter
        assert not board.is_paused(TASK)
        return waited

    assert asyncio.run(scenario()) >= 0.04  # timer ticks are coarse on Windows


def test_a_pause_nobody_ends_is_ended_after_the_limit() -> None:
    async def scenario() -> tuple[float, bool]:
        board = SteeringBoard()
        board.pause(TASK)
        waited = await board.wait_while_paused(TASK, limit=0.05)
        return waited, board.is_paused(TASK)

    waited, still_paused = asyncio.run(scenario())
    assert waited >= 0.04 and not still_paused


def test_forgetting_a_task_frees_whatever_waits_on_it() -> None:
    async def scenario() -> None:
        board = SteeringBoard()
        board.pause(TASK)
        board.push(TASK, "never read")
        waiter = asyncio.create_task(board.wait_while_paused(TASK, limit=5.0))
        await asyncio.sleep(0.02)
        board.forget(TASK)
        await asyncio.wait_for(waiter, 1.0)
        assert board.take(TASK) == []

    asyncio.run(scenario())
