"""A hub that goes down ends its fire-and-forget work first (cancel + await, then dispose the
engine). A task dropped in the middle of a query keeps its connection "idle in transaction"
and blocks everything that needs a lock on the touched tables — that was the occasional hang of
this suite (the next test's TRUNCATE waited for it). ``client`` checks it after every test."""

import asyncio
import contextlib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import assert_clean_shutdown
from topcoat_hub.gateway import drain_background, spawn


def _hold_a_transaction(client: TestClient, seconds: float) -> None:
    """A background task that sits inside an open transaction (as an extraction does between
    two statements)."""
    sessionmaker = client.app.state.sessionmaker

    async def hold() -> None:
        async with sessionmaker() as db:
            await db.execute(text("SELECT 1 FROM node_inventory LIMIT 1"))
            await asyncio.sleep(seconds)

    async def start() -> None:
        spawn(hold())
        await asyncio.sleep(0.2)  # let it reach its query

    client.portal.call(start)


def test_a_task_still_running_at_shutdown_is_cancelled_and_leaves_no_connection(
    client: TestClient,
) -> None:
    client.app.state.settings.shutdown_grace_seconds = 0.1
    _hold_a_transaction(client, 60)
    # the ``client`` fixture ends the hub after this test and asserts a clean shutdown: with the
    # task dropped instead of cancelled it fails with the leaked statement


def test_drain_lets_short_work_finish_and_cancels_the_rest(client: TestClient) -> None:
    done: list[str] = []

    async def scenario() -> tuple[int, list[str]]:
        async def quick() -> None:
            await asyncio.sleep(0.05)
            done.append("quick")

        async def stuck() -> None:
            try:
                await asyncio.sleep(60)
            finally:
                done.append("stuck cancelled")

        spawn(quick())
        spawn(stuck())
        return await drain_background(grace=0.5), done

    cancelled, finished = client.portal.call(scenario)
    assert cancelled == 1
    assert sorted(finished) == ["quick", "stuck cancelled"]
    assert client.portal.call(drain_background) == 0  # nothing left


def test_the_check_names_a_connection_that_stays_in_a_transaction(
    client: TestClient, clean_db: str
) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError

    engine = create_engine(clean_db)
    conn = engine.connect()
    conn.execute(text("SELECT 1 FROM node_inventory LIMIT 1"))  # begins a transaction, no end
    try:
        with pytest.raises(AssertionError, match="node_inventory"):
            assert_clean_shutdown(clean_db)
    finally:
        # the check terminated the backend: closing the dead connection says so, as it should
        with contextlib.suppress(OperationalError):
            conn.close()
        engine.dispose()
    assert_clean_shutdown(clean_db)


def test_the_cleanup_after_a_node_disconnects_is_ended_with_the_hub(
    client: TestClient, connect
) -> None:
    """The node session's own cleanup (it writes "offline" and publishes the node) was a task
    the shutdown did not know: dropped in the middle of its query it kept a connection "idle in
    transaction" — the last cause of the suite hang. It is a task of the hub now: drained."""
    from tests.fakenode import eventually, online_node

    node = online_node(client, connect, "node-a", loaded=["small"])
    service = client.app.state.nodes
    seen: list[str] = []

    async def slow_publish(node_id: Any) -> None:
        seen.append("publish")
        await asyncio.sleep(30)

    service.publish_node = slow_publish  # the cleanup stalls inside its last step
    node.close()

    def reached() -> None:
        assert seen == ["publish"]

    eventually(reached, timeout=5)
    cancelled = client.portal.call(drain_background, 0.1)
    assert cancelled == 1  # the stalled cleanup was found, cancelled and awaited
    assert client.portal.call(drain_background, 0.1) == 0
