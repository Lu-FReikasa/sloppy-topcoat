"""The inbox (docs/PLATFORM.md, 3.7): what happened and wants to be read. An unread item is in the
inbox; reading it opens it and files it in the corner of its application, unless it is pinned or
urgent and not settled; whatever is open in the places the inbox looks at becomes an item, and an
item whose matter is settled elsewhere is resolved. Everything is the caller's own (D20)."""

import json
import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub.models import DEV_ADMIN_ID


def add_item(user_id: Any = DEV_ADMIN_ID, **kw: Any) -> str:
    """An item as a producer would leave it, written straight into the table."""
    item_id = str(uuid.uuid4())
    fields = {
        "id": item_id,
        "user_id": str(user_id),
        "application": "chat",
        "kind": "note",
        "title": "Something happened",
        "summary": "",
        "urgent": False,
        **kw,
    }
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO inbox_items (id, user_id, application, kind, title, summary, urgent) "
                "VALUES (:id, :user_id, :application, :kind, :title, :summary, :urgent)"
            ),
            fields,
        )
    return item_id


def listing(
    client: TestClient, view: str = "inbox", who: dict[str, str] = AUTH, **query: str
) -> Any:
    r = client.get("/api/inbox", params={"view": view, **query}, headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def titles(data: Any) -> list[str]:
    return [i["title"] for i in data["items"]]


def test_an_unread_item_is_in_the_inbox_and_reading_it_files_it(client: TestClient) -> None:
    item = add_item(title="Result of the check")
    assert titles(listing(client)) == ["Result of the check"]
    assert listing(client)["counts"] == {"unread": 1, "urgent": 0, "applications": {"chat": 1}}

    r = client.post(f"/api/inbox/{item}/open", headers=AUTH)
    assert r.status_code == 200 and r.json()["read"] is True and r.json()["filed"] is True
    assert listing(client)["items"] == []  # gone from the inbox the moment it is read
    assert titles(listing(client, "filed")) == ["Result of the check"]
    assert listing(client)["counts"]["unread"] == 0


def test_a_pinned_item_stays_on_the_board_and_is_not_filed(client: TestClient) -> None:
    item = add_item(title="Keep this")
    assert (
        client.post(f"/api/inbox/{item}/pin", json={"pinned": True}, headers=AUTH).status_code
        == 200
    )
    client.post(f"/api/inbox/{item}/open", headers=AUTH)
    assert titles(listing(client, "board")) == ["Keep this"]
    assert listing(client, "filed")["items"] == []  # pinned: not put away
    assert listing(client)["items"] == []  # but read

    client.post(f"/api/inbox/{item}/pin", json={"pinned": False}, headers=AUTH)
    assert listing(client, "board")["items"] == []
    assert titles(listing(client, "filed")) == ["Keep this"]  # unpinned after reading: filed


def test_an_urgent_item_stays_until_it_is_resolved(client: TestClient) -> None:
    quiet = add_item(title="Information")
    urgent = add_item(title="Needs a click", urgent=True)
    first = listing(client)
    assert titles(first) == ["Needs a click", "Information"]  # what needs an action comes first
    assert first["counts"]["urgent"] == 1

    r = client.post(f"/api/inbox/{urgent}/open", headers=AUTH).json()
    assert r["read"] is True and r["filed"] is False and r["unresolved"] is True
    client.post(f"/api/inbox/{quiet}/open", headers=AUTH)
    assert titles(listing(client)) == ["Needs a click"]  # still there, although read

    done = client.post(f"/api/inbox/{urgent}/resolve", headers=AUTH).json()
    assert done["unresolved"] is False and done["filed"] is True
    assert listing(client)["items"] == []
    assert set(titles(listing(client, "filed"))) == {"Information", "Needs a click"}


def test_filed_items_are_kept_per_application_until_they_are_deleted(client: TestClient) -> None:
    a = add_item(title="From admin", application="admin")
    b = add_item(title="From dev", application="dev")
    for item in (a, b):
        client.post(f"/api/inbox/{item}/open", headers=AUTH)
    assert titles(listing(client, "filed", application="admin")) == ["From admin"]
    assert titles(listing(client, "filed", application="dev")) == ["From dev"]
    assert client.delete(f"/api/inbox/{a}", headers=AUTH).status_code == 204
    assert listing(client, "filed", application="admin")["items"] == []
    assert client.delete(f"/api/inbox/{a}", headers=AUTH).status_code == 404


def test_the_counts_say_where_the_unread_items_come_from(client: TestClient) -> None:
    add_item(application="admin")
    add_item(application="admin")
    add_item(application="dev", urgent=True)
    r = client.get("/api/inbox/counts", headers=AUTH).json()
    assert r == {"unread": 3, "urgent": 1, "applications": {"admin": 2, "dev": 1}}


def test_nobody_else_can_see_or_touch_an_item(client: TestClient, clean_db: str) -> None:
    _, other = make_user(clean_db, "user-b")
    item = add_item(title="Private")
    assert listing(client, who=other)["items"] == []
    for call in (
        client.post(f"/api/inbox/{item}/open", headers=other),
        client.post(f"/api/inbox/{item}/pin", json={"pinned": True}, headers=other),
        client.post(f"/api/inbox/{item}/resolve", headers=other),
        client.delete(f"/api/inbox/{item}", headers=other),
    ):
        assert call.status_code == 404
    assert titles(listing(client)) == ["Private"]  # untouched


def test_an_open_memory_question_becomes_an_urgent_item_and_settles_with_its_answer(
    client: TestClient,
) -> None:
    question = str(uuid.uuid4())
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO memory_questions "
                "(id, user_id, item_ids, topic, origin, status, context) "
                "VALUES (:id, :u, '[]'::jsonb, 'Where do you live?', 'chat', 'open', :ctx)"
            ),
            {
                "id": question,
                "u": str(DEV_ADMIN_ID),
                "ctx": json.dumps({"question": "Berlin or Hamburg?"}),
            },
        )
    first = listing(client)
    (item,) = first["items"]
    assert item["kind"] == "memory_question" and item["urgent"] is True
    assert item["title"] == "Where do you live?" and item["summary"] == "Berlin or Hamburg?"
    assert listing(client)["counts"]["urgent"] == 1  # looking twice makes no second item
    assert len(listing(client)["items"]) == 1

    with engine.begin() as conn:  # answered on the memory page
        conn.execute(
            text("UPDATE memory_questions SET status = 'answered' WHERE id = :id"), {"id": question}
        )
    after = listing(client)
    assert after["items"] == []  # settled elsewhere: counts as read, filed
    (filed,) = listing(client, "filed")["items"]
    assert filed["unresolved"] is False and filed["read"] is True


def test_the_inbox_is_a_page_of_chat_and_not_a_program_of_its_own(
    client: TestClient,
) -> None:
    """The inbox belongs to Start (key chat): its pages are in that workspace, after the overview
    of the applications, and the desktop has no program of its own for it."""
    r = client.get("/api/ui/bootstrap", headers=AUTH).json()
    programs = {ws["key"]: ws for ws in r["workspaces"]}
    assert "inbox" not in programs
    assert programs["chat"]["modules"][:3] == ["page.home", "page.inbox", "page.chat"]
    for ws in r["workspaces"]:
        assert ws["open"] == ws["modules"][:1]  # each program starts with its first view
    assert r["prefs"]["desktop"]["open"] == ["chat"]


def test_read_all_files_everything_but_what_waits_for_an_action(client: TestClient) -> None:
    add_item(title="Information")
    add_item(title="Of research", application="research")
    add_item(title="Needs a click", urgent=True)
    r = client.post("/api/inbox/read-all", params={"application": "research"}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"filed": 1}
    assert set(titles(listing(client))) == {"Needs a click", "Information"}
    assert client.post("/api/inbox/read-all", headers=AUTH).json() == {"filed": 1}
    assert titles(listing(client)) == ["Needs a click"]  # urgent and open: it stays
    assert set(titles(listing(client, "filed"))) == {"Information", "Of research"}


def test_notes_of_the_second_look_make_no_item_and_old_ones_are_filed(client: TestClient) -> None:
    add_item(title="A second look has a note", kind="review_hint")
    add_item(title="Information")
    # the next look of the inbox files what no longer makes an item
    assert titles(listing(client)) == ["Information"]
    assert "A second look has a note" in titles(listing(client, "filed"))
