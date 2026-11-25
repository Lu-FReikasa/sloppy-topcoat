"""Start gathers the open conversations (2026-11-13): active within a day and not marked done, or
pinned; a click on pin or done is no activity; new activity after "done" opens it again."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update

from tests.conftest import AUTH
from topcoat_hub.api.chat import is_open
from topcoat_hub.models import Conversation


def _conv(updated: datetime, pinned: datetime | None = None, done: datetime | None = None):
    return Conversation(id=uuid.uuid4(), updated_at=updated, pinned_at=pinned, done_at=done)


def test_the_open_rule() -> None:
    now = datetime(2026, 11, 13, 12, tzinfo=UTC)
    assert is_open(_conv(now - timedelta(hours=3)), now)
    assert not is_open(_conv(now - timedelta(hours=25)), now)  # a day without activity: done
    assert is_open(_conv(now - timedelta(days=9), pinned=now - timedelta(days=9)), now)
    assert not is_open(_conv(now - timedelta(hours=3), done=now - timedelta(hours=1)), now)
    # activity after the click opens it again
    assert is_open(_conv(now - timedelta(minutes=5), done=now - timedelta(hours=1)), now)


def test_pin_and_done_are_clicks_that_keep_the_activity_time(client: TestClient) -> None:
    cid = client.post("/api/conversations", json={}, headers=AUTH).json()["id"]
    before = client.get(f"/api/conversations/{cid}", headers=AUTH).json()["updated_at"]
    r = client.patch(f"/api/conversations/{cid}", json={"done": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["open"] is False and r.json()["updated_at"] == before

    async def age() -> None:
        async with client.app.state.sessionmaker() as db:
            old = datetime.now(UTC) - timedelta(days=3)
            await db.execute(
                update(Conversation)
                .where(Conversation.id == uuid.UUID(cid))
                .values(updated_at=old, done_at=None)
            )
            await db.commit()

    client.portal.call(age)
    assert client.get(f"/api/conversations/{cid}", headers=AUTH).json()["open"] is False
    pinned = client.patch(f"/api/conversations/{cid}", json={"pinned": True}, headers=AUTH).json()
    assert pinned["pinned"] is True and pinned["open"] is True
    listed = {c["id"]: c for c in client.get("/api/conversations", headers=AUTH).json()}
    assert listed[cid]["open"] is True
    done = client.patch(f"/api/conversations/{cid}", json={"done": True}, headers=AUTH).json()
    assert done["pinned"] is False and done["open"] is False
