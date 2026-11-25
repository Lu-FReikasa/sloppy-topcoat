"""One queue for the nodes of a pool (R2-Rest): the node is chosen late, nothing waits on a timer,
and the queue has caps. The tests use the real node queues and plain offers, no gateway."""

import asyncio
from dataclasses import dataclass

import pytest

from topcoat_hub.job_queue import PRIORITY_ANSWER, PRIORITY_CHAT, PRIORITY_IDLE, JobQueue
from topcoat_hub.pool_queue import PoolQueue, QueueFull, Seeker, cap_check


@dataclass(frozen=True)
class Offer:
    node_id: str
    name: str


A, B = Offer("a", "node-a"), Offer("b", "node-b")


def make(
    capacity_a: int = 1, capacity_b: int = 1, **caps: int
) -> tuple[PoolQueue, dict[str, JobQueue]]:
    queues = {"a": JobQueue(capacity_a), "b": JobQueue(capacity_b)}
    pool = PoolQueue(lambda offer: queues[offer.node_id], **caps)
    for queue in queues.values():
        queue.on_change = pool.poke
    return pool, queues


async def settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_the_first_node_with_a_free_place_takes_the_request():
    pool, _ = make()
    offer, place = await pool.take([A, B], PRIORITY_ANSWER)
    assert offer == A and place == PRIORITY_ANSWER


async def test_a_busy_node_does_not_hold_a_request_while_another_node_is_free():
    pool, queues = make()
    await pool.take([A, B], PRIORITY_ANSWER)  # node A is busy now
    offer, _ = await pool.take([A, B], PRIORITY_ANSWER)
    assert offer == B  # chosen late: not queued behind node A
    assert queues["a"].running()[PRIORITY_ANSWER] == 1
    assert queues["b"].running()[PRIORITY_ANSWER] == 1


async def test_a_request_waits_without_a_timer_until_any_node_is_free():
    pool, queues = make()
    await pool.take([A, B], PRIORITY_ANSWER)
    await pool.take([A, B], PRIORITY_ANSWER)
    waiter = asyncio.ensure_future(pool.take([A, B], PRIORITY_ANSWER))
    await asyncio.sleep(0.2)  # longer than nothing should matter: no timer moves it
    assert not waiter.done() and pool.depth() == 1
    queues["b"].release(PRIORITY_ANSWER)  # node B frees first: it takes the waiting request
    offer, _ = await asyncio.wait_for(waiter, 1)
    assert offer == B and pool.depth() == 0


async def test_a_request_pinned_to_one_machine_waits_for_that_machine_only():
    pool, queues = make()
    await pool.take([A], PRIORITY_ANSWER)
    waiter = asyncio.ensure_future(pool.take([A], PRIORITY_ANSWER))  # the pool is node A alone
    await settle()
    assert not waiter.done()  # node B is idle, but not in this request's pool
    queues["a"].release(PRIORITY_ANSWER)
    offer, _ = await asyncio.wait_for(waiter, 1)
    assert offer == A


async def test_chat_is_served_before_idle_work_when_a_place_frees():
    pool, queues = make(capacity_b=0)  # (capacity is at least 1)
    await pool.take([A], PRIORITY_ANSWER)
    idle = asyncio.ensure_future(pool.take([A], PRIORITY_IDLE))
    chat = asyncio.ensure_future(pool.take([A], PRIORITY_CHAT))
    await settle()
    queues["a"].release(PRIORITY_ANSWER)
    await asyncio.wait_for(chat, 1)
    assert not idle.done()  # idle work runs alone, after chat is done
    queues["a"].release(PRIORITY_CHAT)
    await asyncio.wait_for(idle, 1)


async def test_idle_work_does_not_start_while_a_chat_runs_on_the_node():
    pool, queues = make(capacity_a=2)
    await pool.take([A], PRIORITY_CHAT)
    idle = asyncio.ensure_future(pool.take([A], PRIORITY_IDLE))
    await settle()
    assert not idle.done()  # a free place, but chat runs there
    queues["a"].release(PRIORITY_CHAT)
    await asyncio.wait_for(idle, 1)


async def test_a_request_that_gives_up_leaves_the_queue_and_holds_no_place():
    pool, queues = make()
    await pool.take([A, B], PRIORITY_ANSWER)
    await pool.take([A, B], PRIORITY_ANSWER)
    waiter = asyncio.ensure_future(pool.take([A, B], PRIORITY_ANSWER))
    await settle()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert pool.depth() == 0
    queues["a"].release(PRIORITY_ANSWER)
    assert queues["a"].running() == [0, 0, 0]  # nothing was granted to the one that left


async def test_the_whole_queue_has_a_depth_and_refuses_at_once_beyond_it():
    pool, _ = make(max_waiting=2)
    await pool.take([A, B], PRIORITY_ANSWER)
    await pool.take([A, B], PRIORITY_ANSWER)
    first = asyncio.ensure_future(pool.take([A, B], PRIORITY_ANSWER))
    second = asyncio.ensure_future(pool.take([A, B], PRIORITY_ANSWER))
    await settle()
    with pytest.raises(QueueFull) as full:
        await pool.take([A, B], PRIORITY_ANSWER)
    assert full.value.scope == "all"
    for f in (first, second):
        f.cancel()


async def test_one_source_cannot_fill_the_queue_for_the_others():
    pool, _ = make(max_per_source=1)
    await pool.take([A, B], PRIORITY_IDLE, source="background", owner="u1")  # runs on A
    await pool.take([A, B], PRIORITY_IDLE, source="background", owner="u1")  # runs on B
    waiting = asyncio.ensure_future(
        pool.take([A, B], PRIORITY_IDLE, source="background", owner="u1")
    )
    await settle()
    with pytest.raises(QueueFull) as full:
        await pool.take([A, B], PRIORITY_IDLE, source="background", owner="u1")
    assert full.value.scope == "source"
    # another person, or another source of the same person, still may wait
    other = asyncio.ensure_future(pool.take([A, B], PRIORITY_IDLE, source="agent", owner="u1"))
    mine = asyncio.ensure_future(pool.take([A, B], PRIORITY_IDLE, source="background", owner="u2"))
    await settle()
    assert pool.depth() == 3
    for f in (waiting, other, mine):
        f.cancel()


def test_a_chat_request_is_never_refused_by_a_cap():
    full = [Seeker(i, PRIORITY_IDLE, 0.0, [A], "background", "u1") for i in range(5)]
    assert cap_check(full, PRIORITY_CHAT, "background", "u1", 3, 1) is None
    assert cap_check(full, PRIORITY_ANSWER, "background", "u1", 3, 1) is not None


def test_a_cap_of_zero_is_off():
    full = [Seeker(i, PRIORITY_IDLE, 0.0, [A], "background", "u1") for i in range(50)]
    assert cap_check(full, PRIORITY_IDLE, "background", "u1", 0, 0) is None


async def test_the_live_view_lists_the_waiting_in_the_order_they_are_served():
    pool, _ = make()
    await pool.take([A, B], PRIORITY_ANSWER)
    await pool.take([A, B], PRIORITY_ANSWER)
    late = asyncio.ensure_future(
        pool.take([A, B], PRIORITY_IDLE, source="background", info={"kind": "x"})
    )
    soon = asyncio.ensure_future(pool.take([A, B], PRIORITY_CHAT, info={"kind": "chat"}))
    await settle()
    view = pool.waiting_jobs()
    assert [j["place"] for j in view] == [PRIORITY_CHAT, PRIORITY_IDLE]
    assert [j["ahead"] for j in view] == [0, 1]
    assert view[0]["nodes"] == ["node-a", "node-b"] and view[1]["source"] == "background"
    for f in (late, soon):
        f.cancel()
