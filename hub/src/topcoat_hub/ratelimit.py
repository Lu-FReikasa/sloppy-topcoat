"""Per-address limits for the endpoints a caller reaches before it is known: failed sign-ins
(auth.py), pairing and node challenges (api/node.py).

In memory, like the node challenges (the hub is a single process, D6). Each address keeps the
times of its last hits inside a sliding window; the number of addresses kept is capped, the one
seen longest ago is forgotten first.
"""

import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable

MAX_ADDRESSES = 10_000


class AddressLimiter:
    """At most ``limit`` hits per address within ``window_seconds``."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        max_addresses: int = MAX_ADDRESSES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._max = max_addresses
        self._clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def _recent(self, address: str) -> deque[float] | None:
        hits = self._hits.get(address)
        if hits is None:
            return None
        cutoff = self._clock() - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            del self._hits[address]
            return None
        return hits

    def blocked(self, address: str) -> bool:
        """Whether the address has used up its hits for now (records nothing)."""
        hits = self._recent(address)
        return hits is not None and len(hits) >= self.limit

    def retry_after(self, address: str) -> int:
        """Seconds until the address may try again (0 when it may now)."""
        hits = self._recent(address)
        if hits is None or len(hits) < self.limit:
            return 0
        return max(1, math.ceil(hits[0] + self.window_seconds - self._clock()))

    def record(self, address: str) -> None:
        """Count one hit of the address."""
        hits = self._recent(address)
        if hits is None:
            while len(self._hits) >= self._max:
                self._hits.popitem(last=False)
            hits = self._hits[address] = deque()
        else:
            self._hits.move_to_end(address)
        hits.append(self._clock())
        # never hold more than the limit: older hits no longer change the answer
        while len(hits) > self.limit:
            hits.popleft()

    def take(self, address: str) -> bool:
        """Count one hit; False when the address was over its limit already (not counted)."""
        if self.blocked(address):
            return False
        self.record(address)
        return True
