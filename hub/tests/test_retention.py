"""Old node events and old audit rows leave; nothing else does."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from tests.fakenode import online_node
from topcoat_hub.models import DEV_ADMIN_ID, Node, NodeEvent, ToolCall
from topcoat_hub.retention import Retention


def _rows(client: TestClient, node_id: uuid.UUID):
    state = client.app.state

    async def count() -> tuple[int, int]:
        async with state.sessionmaker() as db:
            from sqlalchemy import func, select

            events = await db.scalar(select(func.count()).select_from(NodeEvent))
            calls = await db.scalar(select(func.count()).select_from(ToolCall))
            return events or 0, calls or 0

    return client.portal.call(count)


def _seed(client: TestClient, node_id: uuid.UUID, days_ago: list[int]) -> None:
    state = client.app.state
    now = datetime.now(UTC)

    async def go() -> None:
        async with state.sessionmaker() as db:
            for days in days_ago:
                at = now - timedelta(days=days)
                db.add(NodeEvent(node_id=node_id, kind="test", data={}, ts=at, received_at=at))
                db.add(
                    ToolCall(
                        user_id=DEV_ADMIN_ID,
                        node_name="node-a",
                        capability="docker.ps",
                        via="gui",
                        ok=True,
                        created_at=at,
                    )
                )
            await db.commit()

    client.portal.call(go)


def test_old_rows_go_and_recent_ones_stay(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a")
    state = client.app.state

    async def node_id() -> uuid.UUID:
        from sqlalchemy import select

        async with state.sessionmaker() as db:
            return (await db.scalars(select(Node.id).where(Node.name == "node-a"))).one()

    nid = client.portal.call(node_id)
    before = _rows(client, nid)
    # one row of each kind is 10 days old, one 100 days (node events go after 90), one 400 (both go)
    _seed(client, nid, [10, 100, 400])
    settings = client.app.state.settings
    settings.retention_node_events_days = 90
    settings.retention_tool_calls_days = 365
    gone = client.portal.call(Retention(state.sessionmaker, settings).prune)
    assert gone == {"node_events": 2, "tool_calls": 1}
    events, calls = _rows(client, nid)
    assert events == before[0] + 1 and calls == before[1] + 2


def test_zero_days_keeps_for_ever(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a")
    state = client.app.state
    from sqlalchemy import select

    async def node_id() -> uuid.UUID:
        async with state.sessionmaker() as db:
            return (await db.scalars(select(Node.id).where(Node.name == "node-a"))).one()

    nid = client.portal.call(node_id)
    _seed(client, nid, [4000])
    settings = state.settings
    settings.retention_node_events_days = 0
    settings.retention_tool_calls_days = 0
    gone = client.portal.call(Retention(state.sessionmaker, settings).prune)
    assert gone == {"node_events": 0, "tool_calls": 0}


def test_a_big_backlog_goes_in_batches(client: TestClient, connect, monkeypatch) -> None:
    import topcoat_hub.retention as retention

    online_node(client, connect, "node-a")
    state = client.app.state
    from sqlalchemy import select

    async def node_id() -> uuid.UUID:
        async with state.sessionmaker() as db:
            return (await db.scalars(select(Node.id).where(Node.name == "node-a"))).one()

    nid = client.portal.call(node_id)
    monkeypatch.setattr(retention, "BATCH", 3)
    _seed(client, nid, [500] * 8)
    gone = client.portal.call(Retention(state.sessionmaker, state.settings).prune)
    assert gone["node_events"] == 8 and gone["tool_calls"] == 8
