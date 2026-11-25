"""The Akte of a hand-over: every request has one from the start, the report in it carries its
sources and quotes, and a finished one is filed in the person's knowledge base.

The first group is the file as a function (no hub): the quotes are picked from the evidence and
never reworded, the labels follow the language of the question. The second runs a hand-over end to
end."""

import json
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import eventually
from tests.test_agent import use
from tests.test_handover import DOSSIER, conversation, prepare, send, stub_model, tools
from tests.users import make_user
from topcoat_hub import handover_file

CARD: dict[str, Any] = {
    "version": 1,
    "status": "done",
    "incomplete": False,
    "incomplete_reason": None,
    "summary": "Der Container web wird vom Kernel wegen Speichermangels beendet.",
    "findings": [
        {
            "claim": "Der Container web wurde wegen Speichermangels beendet (OOMKilled).",
            "tag": "live",
            "evidence": ["E1"],
        },
        {"claim": "Es gibt kein Limit für den Container db.", "tag": "doc", "evidence": ["E2"]},
    ],
    "assumptions": [
        {"claim": "Der Speicher war knapp.", "confirm_with": "docker.stats", "evidence": []}
    ],
    "proposal": {
        "text": "Das Limit von web auf 512 MB erhöhen.",
        "impact": "web startet neu.",
        "rollback": "Das alte Limit eintragen.",
        "advice_only": True,
    },
    "evidence": [
        {
            "ref": "E1",
            "tag": "live",
            "node": "tools-alpha",
            "capability": "docker.inspect",
            "input": {"container": "web"},
            "excerpt": "Name: web\nState: exited\nOOMKilled: true\nMemoryLimit: 268435456",
            "truncated": False,
        },
        {
            "ref": "E2",
            "tag": "doc",
            "node": "notes",
            "capability": "knowledge.search",
            "input": {"query": "db"},
            "excerpt": "Die Datenbank läuft ohne Speicherlimit.",
            "truncated": False,
        },
    ],
    "coverage": {"asked": ["tools-alpha"], "not_asked": ["tools-beta"]},
    "notes": [],
    "failures": [{"target": "docker.logs on tools-alpha", "message": "timeout"}],
    "calls": 3,
}


def row(**over: Any) -> dict[str, Any]:
    base = {
        "id": "11111111-2222-3333-4444-555555555555",
        "from_application": "chat",
        "to_application": "admin",
        "status": "done",
        "dossier": {
            "question": "Warum startet der Container web ständig neu?",
            "asked": "Schau in die Logs von web.",
            "expected": "Eine kurze Zusammenfassung.",
            "context": [{"role": "user", "text": "web startet ständig neu"}],
            "withheld": [],
        },
        "error": None,
        "created_at": datetime(2026, 10, 28, 10, 0, tzinfo=UTC),
        "sent_at": datetime(2026, 10, 28, 10, 1, tzinfo=UTC),
        "finished_at": datetime(2026, 10, 28, 10, 5, tzinfo=UTC),
    }
    return {**base, **over}


def test_the_quote_is_the_passage_that_shares_the_most_words_with_the_claim() -> None:
    excerpt = "Name: web\nState: exited\nOOMKilled: true\nMemoryLimit: 268435456"
    assert handover_file.best_quote("Der Container wurde mit OOMKilled beendet", excerpt) == (
        "OOMKilled: true"
    )
    # nothing shared: the start of the excerpt, as it stands
    assert handover_file.best_quote("völlig anderes Thema", excerpt) == "Name: web"
    assert handover_file.best_quote("x", "") == ""
    long = "word " * 200
    assert handover_file.best_quote("word", long).endswith("…")
    assert len(handover_file.best_quote("word", long)) <= handover_file.QUOTE_CHARS


def test_the_report_is_written_with_findings_quotes_and_sources() -> None:
    data, md = handover_file.build(row(), CARD)
    assert md.startswith("# Akte: Warum startet der Container web ständig neu?")
    for heading in (
        "## Eckdaten",
        "## Anfrage",
        "## Bericht",
        "### Zusammenfassung",
        "### Befunde",
        "### Annahmen (nicht gesehen, nicht bestätigt)",
        "### Empfehlung (nur ein Rat, nichts wurde geändert)",
        "### Offene Punkte",
        "## Quellen",
        "## Verlauf",
    ):
        assert heading in md, heading
    assert "1. **gerade gesehen** — Der Container web wurde wegen Speichermangels" in md
    assert "(OOMKilled). [E1]" in md
    assert "> „OOMKilled: true“ — E1 (docker.inspect auf tools-alpha)" in md
    assert "2. **aus den Notizen**" in md
    assert "> „Die Datenbank läuft ohne Speicherlimit.“ — E2" in md
    assert "- Nicht geprüft auf: tools-beta" in md
    assert "Nicht abrufbar: docker.logs on tools-alpha: timeout" in md
    assert "**E1** · gerade gesehen · `docker.inspect` auf tools-alpha" in md
    assert "Zitat: „Name: web State: exited OOMKilled: true MemoryLimit: 268435456“" in md
    assert "- 2026-10-28 10:05 UTC — Bericht liegt vor" in md
    assert data["quotes"]["0"][0]["quote"] == "OOMKilled: true" and data["report"] == CARD


def test_the_labels_follow_the_language_of_the_question() -> None:
    english = row()
    english["dossier"] = {**english["dossier"], "question": "Why does the web container restart?"}
    _, md = handover_file.build(english, None)
    assert md.startswith("# File: Why does the web container restart?")
    assert "## Facts" in md and "There is no report yet." in md


def test_a_request_that_is_only_prepared_already_has_its_file() -> None:
    prepared = row(status="proposed", sent_at=None, finished_at=None)
    _, md = handover_file.build(prepared, None)
    assert "| Stand | vorbereitet |" in md and "Es liegt noch kein Bericht vor." in md
    assert "Abgeschickt" not in md and "- 2026-10-28 10:00 UTC — Anfrage vorbereitet" in md


def test_a_failed_hand_over_keeps_its_error_in_the_file() -> None:
    _, md = handover_file.build(row(status="failed", error="no tool node connected"), None)
    assert "**Fehler:** no tool node connected" in md and "Arbeit fehlgeschlagen" in md


def test_the_file_name_is_readable_and_safe() -> None:
    path = handover_file.knowledge_path(
        "11111111-2222-3333-4444-555555555555",
        "Warum startet der Container web ständig neu?",
        datetime(2026, 10, 28, tzinfo=UTC),
    )
    assert path == "akte-20261028-warum-startet-der-container-web-staendig-11111111.md"


def _finish() -> str:
    return json.dumps(
        {
            "action": "finish",
            "summary": "The web container ran out of memory.",
            "findings": [],
            "next": "",
        }
    )


def _akten(client: TestClient, files: int) -> list[dict[str, Any]]:
    sources = client.get("/api/knowledge/sources", headers=AUTH).json()
    akten = [s for s in sources if s["name"] == "Akten"]
    assert len(akten) == 1 and akten[0]["files"] == files, akten
    return akten


def _filed(client: TestClient, handover_id: str) -> str:
    path = client.get(f"/api/handovers/{handover_id}/file", headers=AUTH).json()["knowledge_path"]
    assert path, "not filed yet"
    return str(path)


def _done(client: TestClient, handover_id: str) -> dict[str, Any]:
    got = client.get(f"/api/handovers/{handover_id}", headers=AUTH).json()
    assert got["status"] == "done", got["status"]
    return got


def test_the_file_exists_from_the_start_and_grows_with_the_work(
    client: TestClient, connect: Any
) -> None:
    tools(client, connect)
    stub_model(client, "The container ran out of memory.")
    sender = conversation(client)
    view = prepare(client, sender).json()
    assert view["has_file"] is True
    prepared = client.get(f"/api/handovers/{view['id']}/file", headers=AUTH).json()
    assert DOSSIER["question"] in prepared["markdown"]
    assert prepared["file"]["report"] is None

    use(client, _finish())
    assert send(client, view).status_code == 200
    eventually(lambda: _done(client, view["id"]), timeout=15)
    done = client.get(f"/api/handovers/{view['id']}/file", headers=AUTH).json()
    # the run looked at nothing, so its report says so instead of dressing up a claim
    assert "## Report" in done["markdown"] and "### Open points" in done["markdown"]
    assert "no tool result was collected" in done["markdown"]
    assert done["file"]["report"]["status"] == "done"

    # a finished hand-over is filed in the person's knowledge base, in the private source "Akten"
    akten = eventually(lambda: _akten(client, 1), timeout=10)
    assert akten[0]["visibility"] == "private"
    path = eventually(lambda: _filed(client, view["id"]), timeout=10)
    assert path.startswith("akte-") and path.endswith(".md")
    # a second hand-over joins the same source
    second = prepare(client, conversation(client)).json()
    use(client, _finish())
    assert send(client, second).status_code == 200
    eventually(lambda: _done(client, second["id"]), timeout=15)
    eventually(lambda: _akten(client, 2), timeout=10)


def test_a_hand_over_from_before_the_file_gets_one_when_it_is_asked_for(
    client: TestClient,
) -> None:
    stub_model(client)
    view = prepare(client, conversation(client)).json()
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(text("UPDATE handovers SET file = NULL, file_markdown = NULL"))
    engine.dispose()
    assert client.get(f"/api/handovers/{view['id']}", headers=AUTH).json()["has_file"] is False
    got = client.get(f"/api/handovers/{view['id']}/file", headers=AUTH).json()
    assert DOSSIER["question"] in got["markdown"]
    assert client.get(f"/api/handovers/{view['id']}", headers=AUTH).json()["has_file"] is True


def test_somebody_elses_file_is_not_found(client: TestClient) -> None:
    stub_model(client)
    view = prepare(client, conversation(client)).json()
    _, other = make_user(TEST_DB_URL, "mallory")
    assert client.get(f"/api/handovers/{view['id']}/file", headers=other).status_code == 404


def test_a_web_page_is_quoted_where_the_claim_is_not_at_its_menu() -> None:
    menu = "Manpages Menu Close menu Search Browse Releases 27.04 Toggle side navigation " * 8
    page = (
        menu
        + "Die Option -h gibt Dateigrößen in menschenlesbarem Format aus. "
        + "Weitere Hinweise zum Wiki folgen unten."
    )
    quote = handover_file.best_quote("Mit -h werden Dateigrößen menschenlesbar ausgegeben", page)
    assert "Die Option -h gibt Dateigrößen" in quote
    assert not quote.startswith("Manpages") and quote.count("Manpages") <= 1
