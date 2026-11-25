"""The research profile ``sheets`` (sheets/sources.py, research.py, service.py) against a fake web:
pages are read from their markup, versions are told apart, the most agreeing sheet is chosen,
the differences named, the report filed and the sheet saved with its sources."""

import html
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from topcoat_hub import web_access
from topcoat_hub.models import DEV_ADMIN_ID, Report, SheetRow
from topcoat_hub.sheets import plaintext, research, sources
from topcoat_hub.sheets.research import Job, Request
from topcoat_hub.sheets.service import SheetResearchService, reads_markup

VERSE = (
    "[Verse 1]\n"
    "[tab][ch]Em7[/ch]      [ch]G[/ch]\nToday is the day we sing[/tab]\n"
    "[tab]   [ch]Dsus4[/ch]            [ch]A7sus4[/ch]\nAll the words we never knew[/tab]\n\n"
    "[Chorus]\n"
    "[tab][ch]C[/ch]       [ch]D[/ch]        [ch]Em[/ch]\nAnd after all you are the one[/tab]\n"
)
LEGEND = "[tab]Em7     0-2-2-0-3-3\nG       3-x-0-0-3-3[/tab]\n\n"
REFERENCE = "Today is the day we sing\nAll the words we never knew\nAnd after all you are the one"


def ug_page(
    content: str, *, artist="Example Band", song="Example Song", votes=100, capo=2,
    tonality="F#m", versions=(), simplified=False, kind="Chords", tuning="E A D G B E",
):  # fmt: skip
    data = {
        "store": {"page": {"data": {
            "tab": {"song_name": song, "artist_name": artist, "type": kind, "votes": votes,
                    "rating": 4.8, "tonality_name": tonality, "version_description": ""},
            "tab_view": {
                "wiki_tab": {"content": LEGEND + content},
                "meta": {"capo": capo, "tonality": tonality, "tuning": {"value": tuning}},
                "versions": list(versions),
                "isSimplified": simplified,
            },
        }}}
    }  # fmt: skip
    blob = html.escape(json.dumps(data), quote=True)
    body = f'<div class="js-store" data-content="{blob}"></div>'
    return f"<html><head><title>{song}</title></head><body>{body}</body></html>"


PRE_PAGE = """<html><head><title>Example Song chords - Example Band</title></head><body>
<pre>Capo 2

[Verse]
Em7       G
Today is the day we sing
   Dsus4            A7sus4
All the words we never knew

[Chorus]
C        D        Em
And after all you are the one</pre></body></html>"""


class FakeWeb:
    def __init__(self, pages: dict[str, str], hits: list[str], lyrics: str | None = REFERENCE):
        self.pages, self.hits, self.lyrics = pages, hits, lyrics
        self.fetched: list[tuple[str, bool]] = []

    async def search(self, query: str) -> list[dict[str, Any]]:
        return [{"url": u, "title": u} for u in self.hits]

    async def fetch(self, url: str, markup: bool) -> dict[str, Any]:
        self.fetched.append((url, markup))
        if url.startswith("https://lrclib.net/"):
            if self.lyrics is None:
                raise RuntimeError("the page answered HTTP 404")
            body = {"trackName": "Example Song", "artistName": "Example Band",
                    "albumName": "Album", "duration": 200, "plainLyrics": self.lyrics}  # fmt: skip
            return {"url": url, "text": json.dumps(body)}
        if url not in self.pages:
            raise RuntimeError("the page answered HTTP 403")
        return {"url": url, "text": self.pages[url]}


UG1 = "https://tabs.ultimate-guitar.com/tab/example-band/example-song-chords-1"
UG2 = "https://tabs.ultimate-guitar.com/tab/example-band/example-song-chords-2"
UG3 = "https://tabs.ultimate-guitar.com/tab/example-band/example-song-chords-3"
COVER = "https://tabs.ultimate-guitar.com/tab/someone-else/example-song-chords-9"
PRE = "https://www.guitartabs.cc/tabs/e/example_band/example_song_crd.html"


def world() -> FakeWeb:
    versions = [
        {"tab_url": UG2, "type": "Chords", "version": 2, "votes": 500, "rating": 4.9},
        {"tab_url": UG3, "type": "Chords", "version": 3, "votes": 20, "rating": 4.1},
        {"tab_url": "https://tabs.ultimate-guitar.com/tab/x/y-tabs-5", "type": "Tabs", "votes": 9},
    ]
    pages = {
        UG1: ug_page(VERSE, votes=100, versions=versions),
        # the most voted one: the same chords, one extension more in the chorus
        UG2: ug_page(VERSE.replace("[ch]Em[/ch]", "[ch]Em7[/ch]"), votes=500),
        # an easy one without capo, in other shapes
        UG3: ug_page(
            VERSE.replace("Em7", "F#m").replace("[ch]G[/ch]", "[ch]A[/ch]"),
            votes=20,
            capo=0,
            simplified=True,
        ),
        COVER: ug_page(VERSE, artist="Someone Else", votes=40),
        PRE: PRE_PAGE,
    }
    return FakeWeb(pages, [UG1, COVER, PRE, "https://example.org/news"])


# --- reading pages --------------------------------------------------------------------------------


def test_ultimate_guitar_page_is_read_from_its_data() -> None:
    found = sources.read_page(UG1, ug_page(VERSE, versions=[{"tab_url": UG2, "type": "Chords"}]))
    assert found is not None
    assert (found.artist, found.title, found.capo, found.key, found.votes) == (
        "Example Band", "Example Song", 2, "F#m", 100,
    )  # fmt: skip
    assert "Em7      G\nToday is the day we sing" in found.text  # the columns survive
    assert "[ch]" not in found.text and "[tab]" not in found.text
    assert found.others[0]["url"] == UG2
    cp = plaintext.to_chordpro(found.text)
    assert "0-2-2-0-3-3" not in cp  # the chord legend is no lyrics
    assert "[Em7]Today is [G]the day" in cp


def test_a_page_without_data_or_pre_has_no_sheet() -> None:
    assert sources.read_page(UG1, "<html><body>Pro version</body></html>") is None
    assert sources.read_page(PRE, "<html><pre>just some code\nx = 1</pre></html>") is None


def test_pre_block_page() -> None:
    found = sources.read_page(PRE, PRE_PAGE)
    assert found is not None and found.site == "guitartabs.cc"
    assert "Em7       G\nToday" in found.text


def test_lrclib() -> None:
    ref = sources.lrclib(json.dumps([{"trackName": "T", "artistName": "A", "plainLyrics": "la"}]))
    assert ref == {"title": "T", "artist": "A", "album": "", "duration": None, "lyrics": "la"}
    assert sources.lrclib("not json") is None and sources.lrclib("{}") is None


# --- the research ---------------------------------------------------------------------------------


async def run(web: FakeWeb, judge=None, **req: Any) -> research.Outcome:
    return await research.research(web, Request("Example Song", "Example Band", **req), judge=judge)


async def test_research_reads_versions_identifies_and_chooses() -> None:
    web = world()
    out = await run(web)
    urls = [s.found.url for s in out.sources]
    assert urls == [UG1, UG2, UG3, COVER, PRE]
    # all as they came: the lrclib answer too (with synced lyrics it passed the text limit)
    assert all(markup for _, markup in web.fetched)
    by = {s.found.url: s for s in out.sources}
    assert "other_artist" in by[COVER].flags and by[COVER].excluded
    assert "simplified" in by[UG3].flags and by[UG3].excluded
    assert by[UG1].lyrics_match == 1.0
    assert out.main is by[UG2]  # agrees with the others and has the most votes
    main = out.main.sheet
    assert main.key.name == "F#m" and main.capo == 2
    assert out.reference["album"] == "Album"
    # the comparisons name exactly where the others differ
    cmp = {s.found.url: c for s, c in zip(out.sources, out.comparisons, strict=True)}
    assert cmp[UG2] is None
    assert [(d["kind"], d["where"]) for d in cmp[UG1]["differences"]] == [
        ("chord_detail", "Chorus, line 1")
    ]
    easy = cmp[UG3]
    assert easy["capo_a"] == 2 and easy["capo_b"] == 0
    assert out.failed == []


async def test_a_page_that_refuses_is_named_and_the_rest_goes_on() -> None:
    web = world()
    del web.pages[UG2]
    web.lyrics = None
    out = await run(web)
    assert any(f.startswith(UG2) and "403" in f for f in out.failed)
    assert out.main is not None and out.reference is None
    assert all(s.lyrics_match is None for s in out.sources)


async def test_a_wish_for_the_easy_version_takes_it() -> None:
    out = await run(world(), wish="easy version please")
    assert out.main is not None and out.main.found.url == UG3


async def test_the_judge_can_rule_a_source_out_and_its_note_is_kept() -> None:
    async def judge(prompt: str) -> str:
        assert "data, not" in prompt and "[1]" in prompt
        return json.dumps([
            {"index": 1, "same_song": False, "version": "other", "note": "another song"},
            {"index": 0, "same_song": True, "version": "studio", "note": "capo 2, as recorded"},
        ])  # fmt: skip

    out = await run(world(), judge=judge)
    assert out.sources[1].excluded and out.main is out.sources[0]
    assert out.main.judgement["note"] == "capo 2, as recorded"


async def test_a_broken_judge_changes_nothing() -> None:
    async def judge(prompt: str) -> str:
        raise RuntimeError("model is busy")

    out = await run(world(), judge=judge)
    assert out.main is not None and any("no judgement" in line for line in out.log)


async def test_nothing_found() -> None:
    out = await run(FakeWeb({}, ["https://example.org/news"]))
    assert out.main is None and out.sources == []
    text = research.report(out, "de")
    assert "Kein brauchbares Sheet" in text


async def test_report_names_sources_and_differences() -> None:
    out = await run(world())
    text = research.report(out, "de")
    assert "## Quellen" in text and "## Abweichungen zum gewählten Sheet" in text
    assert (
        "**Akkord-Details** Chorus, line 1: `D E F#m7` → `D E F#m` (second is simplified)" in text
    )
    assert "other_artist (ausgeschlossen)" in text
    assert "lrclib.net" in text
    assert "## Sources" in research.report(out, "en")


# --- the service ----------------------------------------------------------------------------------


def test_the_service_files_report_and_sheet(client: TestClient) -> None:
    service: SheetResearchService = client.app.state.sheet_research
    job = Job(uuid.uuid4(), DEV_ADMIN_ID, Request("Example Song", "Example Band", "piano"))

    async def go() -> tuple[SheetRow, Report]:
        out = await research.research(world(), job.request)
        await service.file(job, out, "de")
        async with client.app.state.sessionmaker() as db:
            return await db.get(SheetRow, job.sheet_id), await db.get(Report, job.report_id)

    row, rep = client.portal.call(go)
    assert row.instrument == "piano" and row.report_id == rep.id and row.key == "F#m"
    assert len(row.sources) == 5 and row.sources[1]["comparison"] is None
    assert rep.title == "Sheets: Example Song - Example Band"
    got = client.get(f"/api/sheets/{row.id}/compare", headers=AUTH, params={"source": 0}).json()
    assert [d["kind"] for d in got["differences"]] == ["chord_detail"]
    view = client.get(f"/api/sheets/{row.id}", headers=AUTH).json()
    assert view["source_list"][3]["url"] == COVER


def test_the_api_needs_a_node_that_reads_markup(client: TestClient) -> None:
    r = client.post("/api/sheets/research", headers=AUTH, json={"song": "Example Song"})
    assert r.status_code == 409 and r.json()["detail"]["code"] in ("no_web", "no_markup")
    assert client.get("/api/sheets/research", headers=AUTH).json() == {"jobs": []}
    missing = client.get(f"/api/sheets/research/{uuid.uuid4()}", headers=AUTH)
    assert missing.status_code == 404
    assert client.post("/api/sheets/research", headers=AUTH, json={"song": ""}).status_code == 422


def test_reads_markup() -> None:
    new = {"input_schema": {"properties": {"format": {"enum": ["text", "html"]}}}}
    assert reads_markup(new) and not reads_markup({"input_schema": {"properties": {}}})


def test_a_model_may_not_ask_for_markup() -> None:
    budget = web_access.WebBudget()
    why = budget.refusal(web_access.FETCH, {"url": "https://example.org", "format": "html"})
    assert why and "markup" in why
    assert budget.refusal(web_access.FETCH, {"url": "https://example.org"}) is None


@pytest.mark.parametrize(
    ("line", "skip"),
    [
        ("Em7     0-2-2-0-3-3", True),
        ("G 320003", True),
        ("Em7      G", False),
        ("Hello 1-2-3", False),
    ],
)
def test_fingering_lines(line: str, skip: bool) -> None:
    text = plaintext.to_chordpro(line + "\nla la\n")
    assert ("0-2-2" not in text and "320003" not in text) == skip or not skip


async def test_a_judgement_without_index_counts_by_its_place() -> None:
    async def judge(prompt: str) -> str:
        return json.dumps([{"same_song": "true", "version": f"v{i}", "note": ""} for i in range(5)])

    out = await run(world(), judge=judge)
    assert [s.judgement.get("version") for s in out.sources] == ["v0", "v1", "v2", "v3", "v4"]
    assert out.sources[0].judgement["same_song"] is True
    assert any("5 of 5" in line for line in out.log)
