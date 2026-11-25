"""The inbox (docs/PLATFORM.md, 3.7): what happened and wants to be read. An unread item is in the
inbox; reading it takes it out, unless it is urgent and not settled; nothing is filed, pinned or
marked (D111); whatever is open in the places the inbox looks at becomes an item, and an
item whose matter is settled elsewhere is resolved. Everything is the caller's own (D20)."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub.inbox import group_of
from topcoat_hub.models import DEV_ADMIN_ID, InboxItem


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


def listing(client: TestClient, who: dict[str, str] = AUTH, **query: str) -> Any:
    r = client.get("/api/inbox", params=query, headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def titles(data: Any) -> list[str]:
    return [i["title"] for i in data["items"]]


def read_titles(user_id: Any = DEV_ADMIN_ID) -> set[str]:
    """The items that were read (they are gone from the list but kept in the table)."""
    with create_engine(TEST_DB_URL).begin() as conn:
        rows = conn.execute(
            text("SELECT title FROM inbox_items WHERE user_id = :u AND read_at IS NOT NULL"),
            {"u": str(user_id)},
        )
        return {r[0] for r in rows}


def test_an_unread_item_is_in_the_inbox_and_reading_it_takes_it_out(client: TestClient) -> None:
    item = add_item(title="Result of the check")
    (shown,) = listing(client)["items"]
    assert shown["title"] == "Result of the check" and shown["group"] == "results"
    assert "pinned" not in shown and "filed" not in shown and "marks" not in shown  # D111
    assert listing(client)["counts"] == {"unread": 1, "urgent": 0, "applications": {"chat": 1}}

    r = client.post(f"/api/inbox/{item}/open", headers=AUTH)
    assert r.status_code == 200 and r.json()["read"] is True
    assert listing(client)["items"] == []  # gone from the inbox the moment it is read
    assert listing(client)["counts"]["unread"] == 0


def test_the_list_comes_in_three_groups(client: TestClient) -> None:
    """What waits for a decision, what went wrong, results; the newest first within a group.
    (Kinds the look settles by their source, like ``proposal``, are covered by ``group_of``.)"""
    add_item(title="Done", kind="task_done")
    add_item(title="Hand-over failed", kind="handover_failed", urgent=True)
    add_item(title="Waiting fix", kind="fix_blocked", urgent=True)
    add_item(title="Resumed", kind="tasks_resumed")
    add_item(title="Reminder", kind="project_reminder", urgent=True)
    assert [(i["group"], i["title"]) for i in listing(client)["items"]] == [
        ("decide", "Reminder"),
        ("decide", "Waiting fix"),
        ("errors", "Hand-over failed"),
        ("results", "Resumed"),
        ("results", "Done"),
    ]


def test_an_item_is_an_error_by_its_kind_and_a_decision_while_it_waits() -> None:
    def item(kind: str, urgent: bool = False, resolved: bool = False) -> InboxItem:
        resolved_at = datetime.now(UTC) if resolved else None
        return InboxItem(kind=kind, urgent=urgent, resolved_at=resolved_at)

    assert group_of(item("proposal", urgent=True)) == "decide"
    assert group_of(item("memory_question", urgent=True)) == "decide"
    assert group_of(item("proposal", urgent=True, resolved=True)) == "results"
    assert group_of(item("task_failed", urgent=True)) == "errors"
    assert group_of(item("task_failed", urgent=True, resolved=True)) == "errors"
    assert group_of(item("review_error")) == "errors"
    assert group_of(item("handover_result")) == "results"


def test_an_urgent_item_stays_until_it_is_resolved(client: TestClient) -> None:
    quiet = add_item(title="Information")
    urgent = add_item(title="Needs a click", urgent=True)
    first = listing(client)
    assert titles(first) == ["Needs a click", "Information"]  # what needs an action comes first
    assert first["counts"]["urgent"] == 1

    r = client.post(f"/api/inbox/{urgent}/open", headers=AUTH).json()
    assert r["read"] is True and r["unresolved"] is True and r["group"] == "decide"
    client.post(f"/api/inbox/{quiet}/open", headers=AUTH)
    assert titles(listing(client)) == ["Needs a click"]  # still there, although read

    done = client.post(f"/api/inbox/{urgent}/resolve", headers=AUTH).json()
    assert done["unresolved"] is False and done["group"] == "results"
    assert listing(client)["items"] == []
    assert read_titles() == {"Information", "Needs a click"}


def test_an_item_can_be_deleted(client: TestClient) -> None:
    a = add_item(title="From admin", application="admin")
    add_item(title="From dev", application="dev")
    assert titles(listing(client, application="admin")) == ["From admin"]
    assert client.delete(f"/api/inbox/{a}", headers=AUTH).status_code == 204
    assert listing(client, application="admin")["items"] == []
    assert titles(listing(client)) == ["From dev"]
    assert client.delete(f"/api/inbox/{a}", headers=AUTH).status_code == 404


def test_there_is_no_board_no_pin_and_no_marks(client: TestClient) -> None:
    """D111: the routes of the board, of pinning and of marks are gone."""
    item = add_item(title="Information")
    pin = client.post(f"/api/inbox/{item}/pin", json={"pinned": True}, headers=AUTH)
    assert pin.status_code in (404, 405)
    assert client.get(f"/api/inbox/{item}/marks", headers=AUTH).status_code in (404, 405)
    assert client.post(
        f"/api/inbox/{item}/marks", json={"kind": "postit"}, headers=AUTH
    ).status_code in (404, 405)
    # an old client asking for the board just gets the inbox
    assert titles(listing(client, view="board")) == ["Information"]


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
    assert after["items"] == []  # settled elsewhere: counts as read
    assert read_titles() == {"Where do you live?"}


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


def test_read_all_reads_everything_but_what_waits_for_an_action(client: TestClient) -> None:
    add_item(title="Information")
    add_item(title="Of research", application="research")
    add_item(title="Needs a click", urgent=True)
    r = client.post("/api/inbox/read-all", params={"application": "research"}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"read": 1}
    assert set(titles(listing(client))) == {"Needs a click", "Information"}
    assert client.post("/api/inbox/read-all", headers=AUTH).json() == {"read": 1}
    assert titles(listing(client)) == ["Needs a click"]  # urgent and open: it stays
    assert read_titles() == {"Information", "Of research"}


def test_notes_of_the_second_look_make_no_item_and_old_ones_are_put_away(
    client: TestClient,
) -> None:
    add_item(title="A second look has a note", kind="review_hint")
    add_item(title="Information")
    # the next look of the inbox puts away what no longer makes an item
    assert titles(listing(client)) == ["Information"]
    assert "A second look has a note" in read_titles()
