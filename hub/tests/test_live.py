import asyncio

import pytest

from topcoat_hub.api.gui import sse_stream
from topcoat_hub.live import EventBus


async def test_bus_fans_out_and_drops_oldest_when_full() -> None:
    bus = EventBus(queue_size=2)
    a, b = bus.subscribe(), bus.subscribe()
    for i in range(3):
        bus.publish("node_updated", {"i": i})
    assert [a.get_nowait()[1]["i"] for _ in range(2)] == [1, 2]
    assert b.qsize() == 2
    bus.unsubscribe(a)
    assert bus.subscriber_count == 1


async def test_sse_stream_frames_and_keepalive() -> None:
    bus = EventBus()
    disconnected = False

    async def is_disconnected() -> bool:
        return disconnected

    gen = sse_stream(bus, is_disconnected, keepalive_seconds=0.05)
    assert (await anext(gen)).startswith("retry: 3000")
    assert bus.subscriber_count == 1
    assert await anext(gen) == ": ping\n\n"

    bus.publish("node_updated", {"id": "x", "state": "available"})
    frame = await anext(gen)
    assert frame == 'event: node_updated\ndata: {"id":"x","state":"available"}\n\n'

    disconnected = True
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(gen), 1)
    assert bus.subscriber_count == 0
