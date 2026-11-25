"""The request slots of a node are handed out by place in the queue, not by arrival."""

import asyncio
import contextlib

from topcoat_hub.gateway import (
    PRIORITY_ANSWER,
    PRIORITY_CHAT,
    PRIORITY_IDLE,
    background_work,
    current_priority,
    interactive_work,
)
from topcoat_hub.job_queue import JobQueue, Waiting, choose_next, effective_place


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_a_chat_request_goes_before_older_background_work() -> None:
    slots = JobQueue(1)
    await slots.acquire(PRIORITY_CHAT)  # the node is busy
    order: list[str] = []

    async def ask(name: str, priority: int) -> None:
        await slots.acquire(priority)
        order.append(name)
        slots.release(priority)

    idle = asyncio.create_task(ask("idle", PRIORITY_IDLE))
    answer = asyncio.create_task(ask("answer", PRIORITY_ANSWER))
    chat = asyncio.create_task(ask("chat", PRIORITY_CHAT))
    await _settle()
    assert slots.waiting() == [PRIORITY_CHAT, PRIORITY_ANSWER, PRIORITY_IDLE]
    slots.release(PRIORITY_CHAT)
    await asyncio.gather(idle, answer, chat)
    assert order == ["chat", "answer", "idle"]


async def test_idle_work_starts_the_moment_nothing_more_important_is_left() -> None:
    slots = JobQueue(4)
    await slots.acquire(PRIORITY_CHAT)
    idle = asyncio.create_task(slots.acquire(PRIORITY_IDLE))
    await _settle()
    assert not idle.done()  # free slots, but a chat request is running
    slots.release(PRIORITY_CHAT)
    await asyncio.wait_for(idle, 1)  # no timer: the release hands the slot on


async def test_idle_work_runs_alone_and_chat_does_not_wait_for_it() -> None:
    slots = JobQueue(4)
    await slots.acquire(PRIORITY_IDLE)
    second = asyncio.create_task(slots.acquire(PRIORITY_IDLE))
    await _settle()
    assert not second.done()
    await asyncio.wait_for(slots.acquire(PRIORITY_CHAT), 1)  # a chat request takes a free slot
    slots.release(PRIORITY_IDLE)
    await _settle()
    assert not second.done()  # the chat request still runs
    slots.release(PRIORITY_CHAT)
    await asyncio.wait_for(second, 1)


async def test_the_same_place_is_served_in_arrival_order() -> None:
    slots = JobQueue(1)
    await slots.acquire(PRIORITY_CHAT)
    order: list[int] = []

    async def ask(i: int) -> None:
        await slots.acquire(PRIORITY_ANSWER)
        order.append(i)
        slots.release(PRIORITY_ANSWER)

    tasks = [asyncio.create_task(ask(i)) for i in range(3)]
    await _settle()
    slots.release(PRIORITY_CHAT)
    await asyncio.gather(*tasks)
    assert order == [0, 1, 2]


async def test_an_asker_that_gives_up_leaves_the_queue_and_frees_nothing_it_did_not_hold() -> None:
    slots = JobQueue(1)
    await slots.acquire(PRIORITY_CHAT)
    with_timeout = asyncio.create_task(asyncio.wait_for(slots.acquire(PRIORITY_IDLE), 0.05))
    await _settle()
    with contextlib.suppress(TimeoutError):
        await with_timeout
    assert slots.waiting() == []
    slots.release(PRIORITY_CHAT)
    assert slots._value == 1
    await asyncio.wait_for(slots.acquire(PRIORITY_IDLE), 1)


async def test_the_marks_set_the_place_in_the_queue() -> None:
    assert current_priority() == PRIORITY_CHAT

    @background_work
    async def idle() -> int:
        return current_priority()

    @background_work(priority=PRIORITY_ANSWER)
    async def after_answer() -> int:
        return current_priority()

    assert await idle() == PRIORITY_IDLE
    assert await after_answer() == PRIORITY_ANSWER
    with interactive_work():
        assert current_priority() == PRIORITY_ANSWER
    assert current_priority() == PRIORITY_CHAT


# --- the choosing function (SCH-03 step 1): pure, no loop, no node ------------------------------


def _w(place: int, seq: int, since: float = 0.0) -> Waiting:
    return Waiting(place, seq, since)


def test_choose_next_takes_the_best_place_then_the_oldest() -> None:
    waiting = [
        _w(PRIORITY_IDLE, 1),
        _w(PRIORITY_ANSWER, 2),
        _w(PRIORITY_CHAT, 4),
        _w(PRIORITY_CHAT, 3),
    ]
    assert choose_next(waiting, [0, 0, 0], 2, now=0.0) == _w(PRIORITY_CHAT, 3)


def test_choose_next_waits_while_every_place_is_taken() -> None:
    assert choose_next([_w(PRIORITY_CHAT, 1)], [1, 1, 0], 2, now=0.0) is None
    assert choose_next([_w(PRIORITY_CHAT, 1)], [1, 0, 0], 2, now=0.0) == _w(PRIORITY_CHAT, 1)


def test_choose_next_idle_work_stays_off_a_node_with_chat_or_answer_work() -> None:
    idle = [_w(PRIORITY_IDLE, 1)]
    assert choose_next(idle, [1, 0, 0], 4, now=0.0) is None
    assert choose_next(idle, [0, 1, 0], 4, now=0.0) is None
    assert choose_next(idle, [0, 0, 1], 4, now=0.0) is None  # idle work runs alone
    assert choose_next(idle, [0, 0, 0], 4, now=0.0) == _w(PRIORITY_IDLE, 1)


def test_choose_next_a_job_that_cannot_start_holds_back_the_ones_behind_it() -> None:
    # the head is idle work blocked by a running chat; nothing behind it may jump the queue
    waiting = [_w(PRIORITY_IDLE, 1), _w(PRIORITY_IDLE, 2)]
    assert choose_next(waiting, [1, 0, 0], 4, now=0.0) is None


def test_choose_next_is_empty_without_waiting_jobs() -> None:
    assert choose_next([], [0, 0, 0], 4, now=0.0) is None


def test_waiting_raises_the_place_one_level_per_period() -> None:
    assert effective_place(PRIORITY_IDLE, 0, 300) == PRIORITY_IDLE
    assert effective_place(PRIORITY_IDLE, 299, 300) == PRIORITY_IDLE
    assert effective_place(PRIORITY_IDLE, 300, 300) == PRIORITY_ANSWER
    assert effective_place(PRIORITY_IDLE, 650, 300) == PRIORITY_CHAT
    assert effective_place(PRIORITY_IDLE, 10_000, 300) == PRIORITY_CHAT  # never better than a chat
    assert effective_place(PRIORITY_IDLE, 10_000, 0) == PRIORITY_IDLE  # 0 switches it off


def test_an_old_idle_job_goes_before_a_fresh_answer_job() -> None:
    old_idle = _w(PRIORITY_IDLE, 1, since=0.0)
    fresh_answer = _w(PRIORITY_ANSWER, 2, since=600.0)
    assert (
        choose_next([old_idle, fresh_answer], [0, 0, 0], 2, now=610.0, aging_seconds=300)
        == old_idle
    )
    assert (
        choose_next([old_idle, fresh_answer], [0, 0, 0], 2, now=610.0, aging_seconds=0)
        == fresh_answer
    )


async def test_the_queue_follows_a_capacity_that_changes() -> None:
    queue = JobQueue(1)
    await queue.acquire(PRIORITY_CHAT)
    second = asyncio.create_task(queue.acquire(PRIORITY_CHAT))
    await _settle()
    assert not second.done()  # one place, taken
    queue.set_capacity(2)  # the node reports a second slot
    await asyncio.wait_for(second, 1)
    assert queue._value == 0
    queue.set_capacity(1)  # a smaller value cancels nothing that runs
    assert queue.running() == [2, 0, 0] and queue._value == -1
    queue.release(PRIORITY_CHAT)
    assert queue._value == 0  # still full: the next start waits for the second release


async def test_aging_lets_old_idle_work_overtake_answer_work_at_the_next_release() -> None:
    now = [0.0]
    queue = JobQueue(1, aging_seconds=300, clock=lambda: now[0])
    await queue.acquire(PRIORITY_CHAT)  # busy
    order: list[str] = []

    async def ask(name: str, priority: int) -> None:
        await queue.acquire(priority)
        order.append(name)
        queue.release(priority)

    idle = asyncio.create_task(ask("idle", PRIORITY_IDLE))
    await _settle()
    now[0] = 400.0  # the idle job has waited more than one period
    answer = asyncio.create_task(ask("answer", PRIORITY_ANSWER))
    await _settle()
    assert queue.waiting() == [PRIORITY_IDLE, PRIORITY_ANSWER]  # by order of service
    queue.release(PRIORITY_CHAT)
    await asyncio.gather(idle, answer)
    assert order == ["idle", "answer"]
