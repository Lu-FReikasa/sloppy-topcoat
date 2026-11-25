"""In-process live state: connected node sessions and the GUI event bus.

Single hub process by design (see ARCHITECTURE: one hub container). If the hub is ever
scaled out, this is the part that needs a shared broker.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from topcoat_hub.protocol import PROTOCOL_VERSION, envelope

log = logging.getLogger(__name__)


class EventBus:
    """Fan-out of GUI events (``node_updated``, ``node_event``) to SSE subscribers."""

    def __init__(self, queue_size: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[tuple[str, dict[str, Any]]]] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue[tuple[str, dict[str, Any]]]:
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[tuple[str, dict[str, Any]]]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                # A slow client loses its oldest update rather than blocking the hub.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait((event, data))


class NodeOffline(Exception):
    pass


class ActionTimeout(Exception):
    pass


class NodeReadOnly(Exception):
    pass


# How many timed-out actions a session remembers to attribute their late results.
MAX_LATE_ACTIONS = 64

# Put into a gateway stream's queue when the node session ends.
STREAM_LOST: dict[str, Any] = {"type": "stream_lost"}


@dataclass
class NodeSession:
    node_id: uuid.UUID
    websocket: WebSocket
    read_only: bool = False
    # Protocol version the node speaks (from its hello); every message to it carries it.
    version: int = PROTOCOL_VERSION
    # The address the hub sees the node connect from (a candidate for a direct model transfer,
    # E2-07); None when the socket does not tell.
    peer_ip: str | None = None
    pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    # Actions the hub stopped waiting for (504) -> action name. The node still answers them;
    # that late result is stored as an event instead of being dropped.
    late: dict[str, str] = field(default_factory=dict)
    # Gateway streams (E1-05): stream_id -> queue of proxy_chunk / proxy_end messages.
    streams: dict[str, asyncio.Queue[dict[str, Any]]] = field(default_factory=dict)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> None:
        if message.get("v") != self.version:
            message = {**message, "v": self.version}
        async with self._send_lock:
            await self.websocket.send_text(json.dumps(message))

    def resolve(self, message: dict[str, Any]) -> bool:
        future = self.pending.pop(message.get("re", ""), None)
        if future is None or future.done():
            return False
        future.set_result(message["body"])
        return True

    def abandon(self, action_id: str, name: str) -> None:
        """Remember an action whose result may still arrive after the hub gave up on it."""
        self.late[action_id] = name
        while len(self.late) > MAX_LATE_ACTIONS:
            del self.late[next(iter(self.late))]

    def pop_late(self, action_id: str) -> str | None:
        """The action name of a late ``action_result`` (None: an id nobody asked for)."""
        return self.late.pop(action_id, None)

    def open_stream(self, stream_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.streams[stream_id] = queue
        return queue

    def close_stream(self, stream_id: str) -> None:
        self.streams.pop(stream_id, None)

    def deliver(self, message: dict[str, Any]) -> bool:
        """Route a proxy_chunk / proxy_end to its stream; False for unknown streams."""
        queue = self.streams.get(message["body"]["stream_id"])
        if queue is None:
            return False
        queue.put_nowait(message)
        return True

    def fail_all(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(NodeOffline())
        self.pending.clear()
        self.late.clear()
        for queue in self.streams.values():
            queue.put_nowait(STREAM_LOST)
        self.streams.clear()


class NodeSessions:
    """Currently connected nodes and request/response correlation for actions."""

    def __init__(self) -> None:
        self._sessions: dict[uuid.UUID, NodeSession] = {}
        # called when a node connects: work that waited for a node may start now
        self.on_connect: list[Callable[[], None]] = []

    def get(self, node_id: uuid.UUID) -> NodeSession | None:
        return self._sessions.get(node_id)

    def is_connected(self, node_id: uuid.UUID) -> bool:
        return node_id in self._sessions

    def connected_ids(self) -> list[uuid.UUID]:
        return list(self._sessions)

    def add(self, session: NodeSession) -> NodeSession | None:
        """Register a session; returns the previous session of the same node, if any."""
        previous = self._sessions.get(session.node_id)
        self._sessions[session.node_id] = session
        for listener in list(self.on_connect):
            listener()
        return previous

    def remove(self, session: NodeSession) -> bool:
        """Remove ``session`` if it is still the current one for its node."""
        if self._sessions.get(session.node_id) is session:
            del self._sessions[session.node_id]
            session.fail_all()
            return True
        session.fail_all()
        return False

    async def dispatch(
        self, node_id: uuid.UUID, name: str, args: dict[str, Any], timeout_seconds: float
    ) -> dict[str, Any]:
        """Send an action and wait for its ``action_result`` body."""
        session = self._sessions.get(node_id)
        if session is None:
            raise NodeOffline()
        if session.read_only:
            raise NodeReadOnly()
        message = envelope("action", {"name": name, "args": args})
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        session.pending[message["id"]] = future
        try:
            await session.send(message)
            return await asyncio.wait_for(future, timeout_seconds)
        except TimeoutError as exc:
            session.abandon(message["id"], name)
            raise ActionTimeout() from exc
        except (RuntimeError, OSError) as exc:  # socket closed while sending
            raise NodeOffline() from exc
        finally:
            session.pending.pop(message["id"], None)
