"""Marks on inbox items and the regular re-check (docs/PLATFORM.md, 3.7): a mark is data on an item,
never a change of its text; a pinned answer is asked for again and leaves a sticker or a Post-It."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import eventually
from tests.test_agent import finish, use
from tests.test_handover import DOSSIER, conversation, prepare, send, stub_model, tools
from tests.test_inbox import add_item
from tests.users import make_user
from topcoat_hub.recheck import Rechecker


def mark(client: TestClient, item: str, kind: str = "postit", who=AUTH, **body: Any) -> Any:
    return client.post(
        f"/api/inbox/{item}/marks", headers=who, json={"kind": kind, "text": "check this", **body}
    )


def item_with_marks(client: TestClient, item: str) -> dict[str, Any]:
    items = client.get("/api/inbox", headers=AUTH).json()["items"]
    return next(i for i in items if i["id"] == item)


def test_a_mark_sits_on_an_item_and_does_not_change_it(client: TestClient) -> None:
    item = add_item(title="Report", summary="The limit is 256 MB.")
    r = mark(client, item, "postit", text="  this number\n changed ", line=3)
    assert r.status_code == 201, r.text
    got = r.json()
    assert got["kind"] == "postit" and got["author"] == "person" and got["line"] == 3
    assert got["text"] == "this number changed"
    shown = item_with_marks(client, item)
    assert [m["id"] for m in shown["marks"]] == [got["id"]]
    assert shown["summary"] == "The limit is 256 MB." and shown["title"] == "Report"


def test_the_marks_of_one_item_can_be_read(client: TestClient, clean_db: str) -> None:
    item = add_item()
    mark(client, item, "sticker", text="second look passed")
    got = client.get(f"/api/inbox/{item}/marks", headers=AUTH).json()["marks"]
    assert [m["text"] for m in got] == ["second look passed"]
    _, other = make_user(clean_db, "reader-b")
    assert client.get(f"/api/inbox/{item}/marks", headers=other).status_code == 404


def test_every_kind_is_allowed_a_highlight_needs_no_text_the_rest_do(client: TestClient) -> None:
    item = add_item()
    for kind in ("comment", "postit", "sticker", "highlight"):
        assert mark(client, item, kind).status_code == 201, kind
    assert mark(client, item, "highlight", text="", line=1).status_code == 201
    assert mark(client, item, "postit", text="  ").status_code == 422
    assert mark(client, item, "doodle").status_code == 422
    assert mark(client, item, "postit", line=-1).status_code == 422
    assert mark(client, str(uuid.uuid4())).status_code == 404


def test_a_mark_can_be_resolved_and_removed(client: TestClient) -> None:
    item = add_item()
    mid = mark(client, item).json()["id"]
    done = client.post(f"/api/inbox/marks/{mid}/resolve", headers=AUTH).json()
    assert done["resolved_at"] is not None
    open_again = client.post(f"/api/inbox/marks/{mid}/resolve?resolved=false", headers=AUTH).json()
    assert open_again["resolved_at"] is None
    assert client.delete(f"/api/inbox/marks/{mid}", headers=AUTH).status_code == 204
    assert client.delete(f"/api/inbox/marks/{mid}", headers=AUTH).status_code == 404
    assert item_with_marks(client, item)["marks"] == []


def test_marks_belong_to_the_person(client: TestClient, clean_db: str) -> None:
    item = add_item()
    mid = mark(client, item).json()["id"]
    _, other = make_user(clean_db, "someone-else")
    assert mark(client, item, who=other).status_code == 404
    assert client.post(f"/api/inbox/marks/{mid}/resolve", headers=other).status_code == 404
    assert client.delete(f"/api/inbox/marks/{mid}", headers=other).status_code == 404
    assert client.get("/api/inbox", headers=other).json()["items"] == []


def test_deleting_an_item_takes_its_marks_with_it(client: TestClient) -> None:
    item = add_item()
    mark(client, item)
    assert client.delete(f"/api/inbox/{item}", headers=AUTH).status_code in (200, 204)
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM item_marks")).scalar_one() == 0
    engine.dispose()


# --- the regular re-check -----------------------------------------------------------------------


def pinned_answer(client: TestClient, connect: Any, summary: str) -> str:
    """A hand-over that came back, pinned, with its original answer: the thing a re-check starts
    from."""
    tools(client, connect)
    stub_model(client, summary=summary)
    use(client, finish("It ran out of memory."))
    view = prepare(client, conversation(client)).json()
    send(client, view)
    eventually(lambda: _done(client, view["id"]), timeout=15)
    item = next(
        i
        for i in client.get("/api/inbox", headers=AUTH).json()["items"]
        if i["kind"] == "handover_result"
    )
    assert item["ref"]["recheck"]["handover"] == view["id"]
    client.post(f"/api/inbox/{item['id']}/pin", headers=AUTH, json={"pinned": True})
    return item["id"]


def _done(client: TestClient, handover_id: str) -> dict[str, Any]:
    got = client.get(f"/api/handovers/{handover_id}", headers=AUTH).json()
    assert got["status"] == "done", got["status"]
    return got


def run(client: TestClient, coro_fn: Any) -> Any:
    return client.portal.call(coro_fn) if client.portal else asyncio.run(coro_fn())


def rechecker(client: TestClient) -> Rechecker:
    state = client.app.state
    return Rechecker(state.sessionmaker, state.handovers, state.settings)


def age(item: str, hours: float) -> None:
    """Make the answer older, so that a re-check is due."""
    engine = create_engine(TEST_DB_URL)
    when = datetime.now(UTC) - timedelta(hours=hours)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE inbox_items SET created_at = :w WHERE id = :i"), {"w": when, "i": item}
        )
    engine.dispose()


def test_a_young_or_unpinned_answer_is_not_asked_for_again(
    client: TestClient, connect: Any
) -> None:
    item = pinned_answer(client, connect, "The container ran out of memory.")
    assert run(client, rechecker(client).due) == []  # younger than an interval
    age(item, 30)
    client.post(f"/api/inbox/{item}/pin", headers=AUTH, json={"pinned": False})
    assert run(client, rechecker(client).due) == []  # not pinned


def test_an_answer_that_still_stands_gets_a_sticker(client: TestClient, connect: Any) -> None:
    item = pinned_answer(client, connect, "The container ran out of memory.")
    age(item, 30)
    use(client, finish("It ran out of memory."))
    assert run(client, rechecker(client).run_once) == 1
    eventually(lambda: _marked(client, item), timeout=15)
    marks = item_with_marks(client, item)["marks"]
    assert marks[-1]["kind"] == "sticker" and marks[-1]["author"] == "recheck"
    assert marks[-1]["text"].startswith("still correct")
    shown = item_with_marks(client, item)
    assert shown["summary"] == "The container ran out of memory."  # the text is never rewritten
    # checked lately: not again, and a re-check is not a hand-over the person sees
    assert run(client, rechecker(client).due) == []
    assert all(
        h["dossier"]["question"] == DOSSIER["question"]
        for h in client.get("/api/handovers", headers=AUTH).json()["handovers"]
    )
    assert len(client.get("/api/handovers", headers=AUTH).json()["handovers"]) == 1


def _marked(client: TestClient, item: str) -> None:
    assert item_with_marks(client, item)["marks"], "no mark yet"


def test_an_answer_that_changed_gets_a_postit_and_a_new_item(
    client: TestClient, connect: Any
) -> None:
    item = pinned_answer(client, connect, "The container ran out of memory.")
    age(item, 30)

    async def changed(system: str, user_text: str, max_tokens: int = 700) -> str | None:
        if "compare" in system:
            return '{"same": false, "change": "now it is the disk that is full"}'
        return "The disk is full."

    client.app.state.handovers._ask = changed
    use(client, finish("The disk is full."))
    assert run(client, rechecker(client).run_once) == 1
    eventually(lambda: _marked(client, item), timeout=15)
    mark_ = item_with_marks(client, item)["marks"][-1]
    assert mark_["kind"] == "postit" and "outdated" in mark_["text"]
    assert "disk" in mark_["text"]
    news = [
        i
        for i in client.get("/api/inbox", headers=AUTH).json()["items"]
        if i["kind"] == "recheck_changed"
    ]
    assert len(news) == 1 and "disk" in news[0]["summary"]
    assert item_with_marks(client, item)["summary"] == "The container ran out of memory."


def test_only_the_last_few_marks_of_the_recheck_are_kept(client: TestClient) -> None:
    item = add_item()

    async def many() -> None:
        from topcoat_hub.marks import RECHECK, MarkStore

        async with client.app.state.sessionmaker() as db:
            from topcoat_hub.models import DEV_ADMIN_ID

            store = MarkStore(db, DEV_ADMIN_ID)
            for n in range(6):
                await store.add(uuid.UUID(item), "sticker", f"still correct {n}", author=RECHECK)
            await store.add(uuid.UUID(item), "postit", "mine")
            await store.drop_old_recheck(uuid.UUID(item))
            await db.commit()

    run(client, many)
    marks = item_with_marks(client, item)["marks"]
    assert sorted(m["author"] for m in marks).count("recheck") == 3
    assert any(m["author"] == "person" for m in marks)  # the person's own are never dropped
