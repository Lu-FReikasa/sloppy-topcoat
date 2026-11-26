"""Setlists of the program Sheets over HTTP (api/sheets.py, sheets/setlists.py): the order of songs
for one evening, each a sheet as that version or as the newest version of its song; created,
renamed, reordered and deleted, and nobody else's."""

import asyncio
import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub.sheets.setlists import Item, SetlistStore

SONG = """\
{{title: {title}}}
{{artist: Example Band}}

[G]Walking down the [D]road
"""


def _sheet(client: TestClient, title: str, headers: dict[str, str] = AUTH) -> str:
    r = client.post("/api/sheets", headers=headers, json={"text": SONG.format(title=title)})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _setlist(client: TestClient, **body: Any) -> dict[str, Any]:
    r = client.post("/api/sheets/setlists", headers=AUTH, json=body)
    assert r.status_code == 201, r.text
    return dict(r.json())


def test_a_setlist_keeps_its_songs_in_order_and_the_version_choice(client: TestClient) -> None:
    a, b, c = (_sheet(client, t) for t in ("Song A", "Song B", "Song C"))
    got = _setlist(
        client,
        name="  Rehearsal  ",
        items=[{"sheet_id": b}, {"sheet_id": a, "newest": True}, {"sheet_id": c}],
    )
    assert got["name"] == "Rehearsal"
    assert got["items"] == [
        {"sheet_id": b, "newest": False},
        {"sheet_id": a, "newest": True},
        {"sheet_id": c, "newest": False},
    ]
    listed = client.get("/api/sheets/setlists", headers=AUTH).json()["setlists"]
    assert [s["id"] for s in listed] == [got["id"]]
    assert listed[0]["items"] == got["items"]
    assert client.get(f"/api/sheets/setlists/{got['id']}", headers=AUTH).json() == listed[0]


def test_a_setlist_is_renamed_reordered_and_deleted(client: TestClient) -> None:
    a, b = _sheet(client, "Song A"), _sheet(client, "Song B")
    sid = _setlist(client, name="Gig", items=[{"sheet_id": a}])["id"]
    r = client.patch(f"/api/sheets/setlists/{sid}", headers=AUTH, json={"name": "Gig Friday"})
    assert r.json()["name"] == "Gig Friday" and len(r.json()["items"]) == 1
    # the same song twice is allowed (an encore); the new order replaces the old one
    order = [{"sheet_id": b}, {"sheet_id": a}, {"sheet_id": b, "newest": True}]
    r = client.patch(f"/api/sheets/setlists/{sid}", headers=AUTH, json={"items": order})
    assert r.status_code == 200, r.text
    assert [(i["sheet_id"], i["newest"]) for i in r.json()["items"]] == [
        (b, False),
        (a, False),
        (b, True),
    ]
    assert r.json()["name"] == "Gig Friday"
    assert client.delete(f"/api/sheets/setlists/{sid}", headers=AUTH).status_code == 204
    assert client.get(f"/api/sheets/setlists/{sid}", headers=AUTH).status_code == 404
    # the sheets stay
    assert client.get(f"/api/sheets/{a}", headers=AUTH).status_code == 200


def test_deleting_a_sheet_takes_it_out_of_its_setlists(client: TestClient) -> None:
    a, b = _sheet(client, "Song A"), _sheet(client, "Song B")
    sid = _setlist(client, name="Gig", items=[{"sheet_id": a}, {"sheet_id": b}])["id"]
    assert client.delete(f"/api/sheets/{a}", headers=AUTH).status_code == 204
    items = client.get(f"/api/sheets/setlists/{sid}", headers=AUTH).json()["items"]
    assert items == [{"sheet_id": b, "newest": False}]


def test_a_setlist_refuses_unknown_sheets_and_empty_names(client: TestClient) -> None:
    a = _sheet(client, "Song A")
    r = client.post(
        "/api/sheets/setlists",
        headers=AUTH,
        json={"name": "Gig", "items": [{"sheet_id": str(uuid.uuid4())}]},
    )
    assert r.status_code == 404
    assert (
        client.post("/api/sheets/setlists", headers=AUTH, json={"name": "   "}).status_code == 422
    )
    sid = _setlist(client, name="Gig", items=[{"sheet_id": a}])["id"]
    r = client.patch(
        f"/api/sheets/setlists/{sid}",
        headers=AUTH,
        json={"items": [{"sheet_id": a}, {"sheet_id": str(uuid.uuid4())}]},
    )
    assert r.status_code == 404
    # a refused change leaves the setlist as it was
    items = client.get(f"/api/sheets/setlists/{sid}", headers=AUTH).json()["items"]
    assert items == [{"sheet_id": a, "newest": False}]
    assert client.get("/api/sheets/setlists", headers=AUTH).json()["setlists"][0]["id"] == sid


def test_setlists_are_personal(client: TestClient) -> None:
    mine = _sheet(client, "Song A")
    sid = _setlist(client, name="Gig", items=[{"sheet_id": mine}])["id"]
    _, other = make_user(TEST_DB_URL, f"setlists-{uuid.uuid4().hex[:6]}")
    assert client.get("/api/sheets/setlists", headers=other).json() == {"setlists": []}
    assert client.get(f"/api/sheets/setlists/{sid}", headers=other).status_code == 404
    r = client.patch(f"/api/sheets/setlists/{sid}", headers=other, json={"name": "x"})
    assert r.status_code == 404
    assert client.delete(f"/api/sheets/setlists/{sid}", headers=other).status_code == 404
    # nobody can put another person's sheet into a setlist of their own
    r = client.post(
        "/api/sheets/setlists", headers=other, json={"name": "Mine", "items": [{"sheet_id": mine}]}
    )
    assert r.status_code == 404
    assert client.get(f"/api/sheets/setlists/{sid}", headers=AUTH).json()["name"] == "Gig"


async def test_two_new_orders_at_once_do_not_collide(client: TestClient) -> None:
    """Two quick reorders (Alt+↓ held, or two tabs) are two PATCHes that may overlap. Each writes
    the whole order; the second waits for the first instead of failing on the positions."""
    a, b = _sheet(client, "Song A"), _sheet(client, "Song B")
    sid = uuid.UUID(_setlist(client, name="Gig", items=[{"sheet_id": a}, {"sheet_id": b}])["id"])
    me = uuid.UUID(client.get("/api/me", headers=AUTH).json()["id"])
    engine = create_async_engine(TEST_DB_URL)
    try:
        async with AsyncSession(engine) as first, AsyncSession(engine) as second:
            await SetlistStore(first, me).update(
                sid, items=[Item(uuid.UUID(b)), Item(uuid.UUID(a))]
            )
            later = asyncio.create_task(
                SetlistStore(second, me).update(sid, items=[Item(uuid.UUID(a), newest=True)])
            )
            await asyncio.sleep(0.3)  # the second update now waits on the first
            await first.commit()
            await later
            await second.commit()
    finally:
        await engine.dispose()
    items = client.get(f"/api/sheets/setlists/{sid}", headers=AUTH).json()["items"]
    assert items == [{"sheet_id": a, "newest": True}]
