"""One queue for the nodes that can serve a request (R2-Rest, the principle of the queue).

A request does not pick a node first and wait there. It names the nodes that may serve it (the
pool, the pick and ``max_loaded`` already decided that) and waits **here**, in one queue, until
any of them has a free place; that node takes the first request in line it can serve. So a request
never waits behind a busy node while another suitable node is idle, and no timer moves, switches or
cancels a waiting request. (The guards against a node that hangs stay where they are: the first
byte and the idle timeout of a running stream.)

The rules of a node stay those of :mod:`topcoat_hub.job_queue`: a lower place (chat < answer <
idle) goes first, idle work runs alone and only while nothing more important runs there, a running
request is never cancelled. What is new is the choice of the node, made late, and the **caps**:
a queue must stay short, so the hub refuses at once (``queue_full``) instead of letting a request
wait for hours:

* a global depth for everything that waits;
* a depth per person and source (background, agent, bench) so one source cannot fill the queue.

A chat request (the best place) is never refused by a cap: a person is waiting for it.
"""

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from topcoat_hub.job_queue import (
    PRIORITY_CHAT,
    JobQueue,
    clamp_place,
    effective_place,
)


class Offer(Protocol):
    """What the queue needs to know about a node a request may use (the gateway's Candidate)."""

    node_id: Any
    name: str


class QueueFull(Exception):
    """A cap is reached: ``scope`` is ``"all"`` or ``"source"``."""

    def __init__(self, scope: str, depth: int, limit: int) -> None:
        super().__init__(f"queue full ({scope}: {depth} of {limit})")
        self.scope, self.depth, self.limit = scope, depth, limit


@dataclass
class Seeker:
    """One request that waits for a place on any of its nodes."""

    seq: int
    place: int
    since: float
    offers: list[Offer]
    source: str  # who asked: "chat", "agent", "background", "bench"
    owner: str  # the person (or "" for the hub itself): the cap per source is per person
    info: dict[str, Any] = field(default_factory=dict)
    future: "asyncio.Future[Offer]" = field(default=None)  # type: ignore[assignment]
    granted_place: int = -1  # the place the grant was taken at (aging can make it better)


def cap_check(
    waiting: Sequence[Seeker], place: int, source: str, owner: str, max_all: int, max_source: int
) -> QueueFull | None:
    """Whether a new request may join. A chat request (the best place) always may; a limit of 0
    switches that cap off."""
    if place <= PRIORITY_CHAT:
        return None
    if max_all > 0 and len(waiting) >= max_all:
        return QueueFull("all", len(waiting), max_all)
    same = sum(1 for w in waiting if w.source == source and w.owner == owner)
    if max_source > 0 and same >= max_source:
        return QueueFull("source", same, max_source)
    return None


class PoolQueue:
    """The waiting requests of all nodes, and the grant of a place on whichever node frees up.

    ``queue_of`` gives the places of a node (a :class:`JobQueue` that nothing waits in: the places
    are only taken with :meth:`JobQueue.try_acquire` and given back with ``release``). A grant
    holds the place for the asker: it must release it on that node when the request is over.
    """

    def __init__(
        self,
        queue_of: Callable[[Offer], JobQueue],
        *,
        max_waiting: int = 0,
        max_per_source: int = 0,
        aging_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._queue_of = queue_of
        self.max_waiting, self.max_per_source = max_waiting, max_per_source
        self.aging_seconds = aging_seconds
        self._clock = clock
        self._waiting: dict[int, Seeker] = {}
        self._seq = 0
        self._granting = False
        self._again = False

    # --- the view ------------------------------------------------------------------------

    def depth(self) -> int:
        return sum(1 for s in self._waiting.values() if not s.future.done())

    def waiting_jobs(self) -> list[dict[str, Any]]:
        """The requests that wait now in the order they will be served, for the live view: place,
        seconds waited, the work they are, who is ahead of them, and the nodes they may use."""
        now = self._clock()
        live = [s for s in self._waiting.values() if not s.future.done()]
        ordered = self._ordered(live, now)
        return [
            {
                "place": s.place,
                "seconds": max(0, int(now - s.since)),
                "ahead": index,
                "source": s.source,
                "nodes": [o.name for o in s.offers],
                **s.info,
            }
            for index, s in enumerate(ordered)
        ]

    # --- waiting -------------------------------------------------------------------------

    def _ordered(self, seekers: Sequence[Seeker], now: float) -> list[Seeker]:
        return sorted(
            seekers,
            key=lambda s: (effective_place(s.place, now - s.since, self.aging_seconds), s.seq),
        )

    def poke(self) -> None:
        """Something changed (a place was freed, a node came or went): serve whoever can start."""
        self._grant()

    def _grant(self) -> None:
        # looking up the places of a node can change its capacity, which calls poke() again: that
        # call only asks for another round, the round in progress is never entered twice
        if self._granting:
            self._again = True
            return
        self._granting = True
        try:
            while True:
                self._again = False
                self._grant_round()
                if not self._again:
                    return
        finally:
            self._granting = False

    def _grant_round(self) -> None:
        for seq, seeker in list(self._waiting.items()):
            if seeker.future.done():  # its asker gave up
                del self._waiting[seq]
        now = self._clock()
        for seeker in self._ordered(list(self._waiting.values()), now):
            place = effective_place(seeker.place, now - seeker.since, self.aging_seconds)
            for offer in seeker.offers:
                if self._queue_of(offer).try_acquire(place):
                    self._waiting.pop(seeker.seq, None)
                    seeker.granted_place = place
                    seeker.future.set_result(offer)
                    break

    async def take(
        self,
        offers: Sequence[Offer],
        priority: int,
        *,
        source: str = "chat",
        owner: str = "",
        info: dict[str, Any] | None = None,
    ) -> tuple[Offer, int]:
        """Wait for a place on any of ``offers`` (best first, so a free place on an earlier one
        wins) and return the offer whose place is held, with the place it was taken at. Raises
        :class:`QueueFull` at once when a cap is reached."""
        place = clamp_place(priority)
        full = cap_check(
            [s for s in self._waiting.values() if not s.future.done()],
            place,
            source,
            owner,
            self.max_waiting,
            self.max_per_source,
        )
        if full is not None:
            raise full
        self._seq += 1
        seeker = Seeker(
            self._seq,
            place,
            self._clock(),
            list(offers),
            source,
            owner,
            info or {},
            asyncio.get_running_loop().create_future(),
        )
        self._waiting[seeker.seq] = seeker
        self._grant()
        try:
            offer = await seeker.future
        except BaseException:
            if seeker.future.done() and not seeker.future.cancelled():
                # granted at the same moment the asker left: give the place back
                self._queue_of(seeker.future.result()).release(seeker.granted_place)
            else:
                seeker.future.cancel()
            self._waiting.pop(seeker.seq, None)
            self._grant()
            raise
        # the place that is given back later is the one the grant was taken at
        return offer, seeker.granted_place
