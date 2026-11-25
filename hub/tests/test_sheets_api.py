"""The program Sheets over HTTP (api/sheets.py): a sheet is stored in sounding pitch, shown per
instrument with or without the capo, changed, compared with its sources, and nobody else's."""

import uuid
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub.models import DEV_ADMIN_ID, SheetRow
from topcoat_hub.sheets.chordpro import parse
from topcoat_hub.sheets.render import render

# G shapes with capo 2: it sounds in A
SONG = """\
{title: Example Song}
{artist: Example Band}
{capo: 2}

{start_of_verse: Verse 1}
[G]Walking down the [D]road
[Em]Nothing on my [C]mind
{end_of_verse}

{start_of_chorus}
[C]Sing it [D]loud
{end_of_chorus}

{chorus}
"""

PLAIN = """\
Capo 2

[Verse 1]
G               D
Walking down the road
"""


def _create(client: TestClient, headers: dict[str, str] = AUTH, **body: Any) -> dict[str, Any]:
    r = client.post("/api/sheets", headers=headers, json={"text": SONG, **body})
    assert r.status_code == 201, r.text
    return r.json()


def _chords(view: dict[str, Any], section: int = 0, line: int = 0) -> list[str]:
    segs = view["sections"][section]["lines"][line]["segments"]
    return [s["chord"] for s in segs if s["chord"]]


def test_a_sheet_is_stored_in_sounding_pitch_and_shown_with_its_capo(client: TestClient) -> None:
    got = _create(client)
    assert (got["title"], got["key"], got["capo"], got["instrument"]) == (
        "Example Song",
        "A",
        2,
        "guitar",
    )
    view = got["view"]
    assert view["capo_view"] is True and view["shown_key"] == "G" and view["key"] == "A"
    assert _chords(view) == ["G", "D"]
    assert [f["kind"] for f in view["flow"]] == ["verse", "chorus", "chorus"]
    assert view["flow"][2]["repeat_of"] == 1

    async def stored() -> str:
        async with client.app.state.sessionmaker() as db:
            row = await db.get(SheetRow, uuid.UUID(got["id"]))
            return row.chordpro

    text = client.portal.call(stored)
    assert "{x_capo: 2}" in text and "[A]Walking" in text


def test_the_capo_view_can_be_switched_and_stays(client: TestClient) -> None:
    sid = _create(client)["id"]
    once = client.get(f"/api/sheets/{sid}", headers=AUTH, params={"capo_view": False}).json()
    assert _chords(once["view"]) == ["A", "E"] and once["view"]["capo"] == 2
    r = client.patch(f"/api/sheets/{sid}", headers=AUTH, json={"capo_view": False})
    assert r.status_code == 200
    again = client.get(f"/api/sheets/{sid}", headers=AUTH).json()
    assert again["capo_view"] is False and _chords(again["view"]) == ["A", "E"]


def test_piano_shows_what_sounds_without_capo(client: TestClient) -> None:
    sid = _create(client, instrument="piano")["id"]
    view = client.get(f"/api/sheets/{sid}", headers=AUTH).json()["view"]
    assert view["instrument"] == "piano" and view["capo"] == 0
    assert _chords(view) == ["A", "E"]


def test_moving_the_capo_keeps_the_sound(client: TestClient) -> None:
    sid = _create(client)["id"]
    got = client.patch(f"/api/sheets/{sid}", headers=AUTH, json={"capo": 0}).json()
    assert got["key"] == "A" and got["capo"] == 0
    assert _chords(got["view"]) == ["A", "E"]
    got = client.patch(f"/api/sheets/{sid}", headers=AUTH, json={"capo": 4}).json()
    assert got["view"]["shown_key"] == "F" and _chords(got["view"]) == ["F", "C"]


def test_transpose_in_the_view_and_for_good(client: TestClient) -> None:
    sid = _create(client)["id"]
    shown = client.get(
        f"/api/sheets/{sid}", headers=AUTH, params={"transpose": 1, "capo_view": False}
    ).json()
    assert shown["view"]["key"] == "Bb" and _chords(shown["view"]) == ["Bb", "F"]
    assert client.get(f"/api/sheets/{sid}", headers=AUTH).json()["key"] == "A"  # not stored
    got = client.patch(f"/api/sheets/{sid}", headers=AUTH, json={"transpose": -2}).json()
    assert got["key"] == "G" and got["capo"] == 2
    assert got["view"]["shown_key"] == "F"


def test_a_plain_page_becomes_a_sheet(client: TestClient) -> None:
    got = _create(client, text=PLAIN, format="plain", title="Example Song")
    assert got["capo"] == 2 and got["key"] == "A"
    view = got["view"]
    segs = view["sections"][0]["lines"][0]["segments"]
    assert [(s["chord"], s["text"]) for s in segs] == [("G", "Walking down the "), ("D", "road")]


def test_refusals(client: TestClient) -> None:
    def post(**body: Any) -> int:
        return client.post("/api/sheets", headers=AUTH, json=body).status_code

    assert post(text="no chords at all", title="x") == 422
    assert post(text="[G]la", title="") == 422  # no title anywhere
    assert post(text=SONG, instrument="drums") == 422
    sid = _create(client)["id"]
    assert client.patch(f"/api/sheets/{sid}", headers=AUTH, json={"capo": 13}).status_code == 422
    r = client.get(f"/api/sheets/{sid}/compare", headers=AUTH, params={"source": 0})
    assert r.status_code == 404
    assert client.get(f"/api/sheets/{sid}/compare", headers=AUTH).status_code == 422


def test_compare_two_sheets(client: TestClient) -> None:
    a = _create(client)["id"]
    other = SONG.replace("{capo: 2}", "{capo: 0}").replace("[Em]Nothing on my", "[Em]Nothing in my")
    b = _create(client, text=other)["id"]
    got = client.get(f"/api/sheets/{a}/compare", headers=AUTH, params={"other": b}).json()
    assert got["against"] == {"sheet": b}
    assert got["shift"] == -2  # the other one sounds in G
    kinds = [d["kind"] for d in got["differences"]]
    assert kinds == ["key", "capo", "lyrics"]
    assert got["differences"][2]["where"] == "Verse 1, line 2"


def test_compare_with_a_source(client: TestClient) -> None:
    sid = _create(client)["id"]

    async def add_source() -> None:
        async with client.app.state.sessionmaker() as db:
            row = await db.get(SheetRow, uuid.UUID(sid))
            simple = row.chordpro.replace("[A]Walking", "[A]Walking")
            row.sources = [
                {"url": "https://chords.example/song", "title": "Example", "version": "studio",
                 "chordpro": simple.replace("[E]road", "[E7]road")}
            ]  # fmt: skip
            await db.commit()

    client.portal.call(add_source)
    sheet = client.get(f"/api/sheets/{sid}", headers=AUTH).json()
    assert sheet["sources"] == 1
    assert sheet["source_list"][0]["url"] == "https://chords.example/song"
    got = client.get(f"/api/sheets/{sid}/compare", headers=AUTH, params={"source": 0}).json()
    assert [(d["kind"], d["a"], d["b"]) for d in got["differences"]] == [
        ("chord_detail", "A E", "A E7")
    ]


def test_sheets_are_personal(client: TestClient) -> None:
    sid = _create(client)["id"]
    _, other = make_user(TEST_DB_URL, f"sheets-{uuid.uuid4().hex[:6]}")
    assert client.get(f"/api/sheets/{sid}", headers=other).status_code == 404
    assert client.patch(f"/api/sheets/{sid}", headers=other, json={"capo": 0}).status_code == 404
    assert client.delete(f"/api/sheets/{sid}", headers=other).status_code == 404
    assert client.get("/api/sheets", headers=other).json() == {"sheets": []}
    mine = client.get("/api/sheets", headers=AUTH).json()["sheets"]
    assert any(s["id"] == sid for s in mine)
    assert client.delete(f"/api/sheets/{sid}", headers=AUTH).status_code == 204
    assert client.get(f"/api/sheets/{sid}", headers=AUTH).status_code == 404
    assert DEV_ADMIN_ID  # the dev admin owns what AUTH writes


def test_render_rejects_an_unknown_instrument() -> None:
    try:
        render(parse(SONG), "drums")
    except ValueError:
        return
    raise AssertionError("render took an unknown instrument")
