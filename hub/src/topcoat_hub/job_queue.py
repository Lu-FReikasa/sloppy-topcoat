"""The queue of model requests and the rule that picks the next one (SCH-03, SCHEDULING.md 3).

Two parts, so the rule can be tested without a loop, a database or a node:

* :func:`choose_next` is a pure function. Given the jobs that wait, what runs and how many places
  the node has, it says which job starts now, if any.
* :class:`JobQueue` keeps the waiting jobs of one node, calls :func:`choose_next` whenever something
  arrives or ends, and wakes the asker of the job it picked. It has the interface the gateway has
  always used (``acquire``, ``release``, ``waiting``), so callers do not change.

The rules, as they were in ``PrioritySlots`` and stay for step 1 of SCH-03:

* a lower place (``PRIORITY_CHAT`` < ``PRIORITY_ANSWER`` < ``PRIORITY_IDLE``) goes first, within a
  place the first to ask goes first;
* idle work runs alone and only while no chat or answer work runs on the node;
* a running request is never cancelled to make room.

New: the number of places is not a constant but follows what the node reports (``n_parallel`` of
its loaded models), and waiting can raise a job by one place every ``aging_seconds`` (0 = off) so
that nothing starves.
"""

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

PRIORITY_CHAT, PRIORITY_ANSWER, PRIORITY_IDLE = 0, 1, 2


def clamp_place(priority: int) -> int:
    return min(max(priority, PRIORITY_CHAT), PRIORITY_IDLE)


@dataclass(frozen=True)
class Waiting:
    """One job in the queue, as the choosing function sees it."""

    place: int
    seq: int  # arrival number: the tie-break inside a place
    since: float  # when it started to wait, on the queue's clock
    # what the live view says about it (work label, model, application, conversation ...): never
    # looked at by the choosing rule
    info: dict[str, Any] = field(default_factory=dict, compare=False)


def effective_place(place: int, waited: float, aging_seconds: float) -> int:
    """The place a job counts with after waiting ``waited`` seconds: one better for every
    ``aging_seconds`` (never better than ``PRIORITY_CHAT``); ``aging_seconds <= 0`` is off."""
    if aging_seconds <= 0:
        return place
    return max(PRIORITY_CHAT, place - int(max(0.0, waited) // aging_seconds))


def can_start(place: int, running: Sequence[int], capacity: int) -> bool:
    """Whether a job of this (effective) place may start now: a free place on the node, and idle
    work only when nothing more important runs and no other idle job runs."""
    if capacity - sum(running) <= 0:
        return False
    if place < PRIORITY_IDLE:
        return True
    return not running[PRIORITY_IDLE] and not (running[PRIORITY_CHAT] or running[PRIORITY_ANSWER])


def order_waiting(waiting: Sequence[Waiting], now: float, aging_seconds: float) -> list[Waiting]:
    """The jobs in the order they will be served: best (aged) place first, then arrival."""
    return sorted(
        waiting, key=lambda w: (effective_place(w.place, now - w.since, aging_seconds), w.seq)
    )


def choose_next(
    waiting: Sequence[Waiting],
    running: Sequence[int],
    capacity: int,
    now: float,
    aging_seconds: float = 0.0,
) -> Waiting | None:
    """The job that starts now, or None. Only the head of the queue is considered: a job that
    cannot start yet holds back the ones behind it, so important work is not overtaken."""
    ordered = order_waiting(waiting, now, aging_seconds)
    if not ordered:
        return None
    head = ordered[0]
    if can_start(effective_place(head.place, now - head.since, aging_seconds), running, capacity):
        return head
    return None


class JobQueue:
    """The request places of one node, handed out by :func:`choose_next` instead of by arrival.

    ``capacity`` is the number of requests the node can serve at once; the gateway sets it from
    what the node reports (:meth:`set_capacity`). It can change while jobs wait or run: a smaller
    value never cancels a running job, it only holds back the next start.
    """

    def __init__(
        self,
        capacity: int,
        *,
        aging_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = max(1, capacity)
        self.aging_seconds = aging_seconds
        self._clock = clock
        self._running = [0, 0, 0]
        self._waiting: dict[int, tuple[Waiting, asyncio.Future[None]]] = {}
        self._seq = 0
        # called when a place is freed or the capacity changes (the pool's queue serves next)
        self.on_change: Callable[[], None] | None = None

    @property
    def size(self) -> int:  # the name PrioritySlots had
        return self.capacity

    @property
    def _value(self) -> int:  # free places; the name of asyncio.Semaphore's counter
        return self.capacity - sum(self._running)

    def set_capacity(self, capacity: int) -> None:
        capacity = max(1, capacity)
        if capacity != self.capacity:
            self.capacity = capacity
            self._grant()
            if self.on_change is not None:
                self.on_change()

    def waiting(self) -> list[int]:
        """The places of the requests that wait now, in the order they will be served."""
        live = [w for w, fut in self._waiting.values() if not fut.done()]
        return [w.place for w in order_waiting(live, self._clock(), self.aging_seconds)]

    def waiting_jobs(self) -> list[dict[str, Any]]:
        """The jobs that wait now, in the order they will be served: their place, how long they
        have waited and the facts their asker gave (for the live view)."""
        now = self._clock()
        live = [w for w, fut in self._waiting.values() if not fut.done()]
        return [
            {"place": w.place, "seconds": max(0, int(now - w.since)), **w.info}
            for w in order_waiting(live, now, self.aging_seconds)
        ]

    def running(self) -> list[int]:
        """How many requests run in each place."""
        return list(self._running)

    def _grant(self) -> None:
        while True:
            for seq, (_, fut) in list(self._waiting.items()):
                if fut.done():  # its asker gave up
                    del self._waiting[seq]
            live = [w for w, _ in self._waiting.values()]
            picked = choose_next(
                live, self._running, self.capacity, self._clock(), self.aging_seconds
            )
            if picked is None:
                return
            _, fut = self._waiting.pop(picked.seq)
            self._running[picked.place] += 1
            fut.set_result(None)

    async def acquire(
        self, priority: int = PRIORITY_CHAT, info: dict[str, Any] | None = None
    ) -> None:
        place = clamp_place(priority)
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        self._waiting[self._seq] = (Waiting(place, self._seq, self._clock(), info or {}), fut)
        self._grant()
        try:
            await fut
        except BaseException:
            if fut.done() and not fut.cancelled():
                self._running[place] -= 1  # granted at the same moment the asker left
            else:
                fut.cancel()
            self._grant()
            raise

    def try_acquire(self, priority: int = PRIORITY_CHAT) -> bool:
        """Take a place now if the rule allows it, else leave everything as it is (the asker waits
        elsewhere: in the pool's queue, which chooses among the nodes). A job that waits in this
        queue and comes before is respected: it is served first."""
        place = clamp_place(priority)
        if any(not fut.done() for _, fut in self._waiting.values()):
            return False
        if not can_start(place, self._running, self.capacity):
            return False
        self._running[place] += 1
        return True

    def release(self, priority: int = PRIORITY_CHAT) -> None:
        place = clamp_place(priority)
        self._running[place] = max(0, self._running[place] - 1)
        self._grant()
        if self.on_change is not None:
            self.on_change()
