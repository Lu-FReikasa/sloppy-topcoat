"""An issue's investigation in rounds (issue_investigation.py): the lead asks Development and
Verwaltung until it writes a report; a plan that misses the code goes back to it as a round; the
settled plan is what "Plan umsetzen" carries out."""

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from topcoat_hub import issue_investigation as ii
from topcoat_hub.models import DEV_ADMIN_ID as USER
from topcoat_hub.models import AgentTask
from topcoat_hub.project_store import ProjectStore

PATH = "hub/src/topcoat_hub/x.py"
LIVE: dict[str, str | None] = {PATH: "def parse(text):\n    return text.strip()\n"}


async def read(paths: list[str]) -> dict[str, str | None]:
    return {p: LIVE.get(p) for p in paths}


def report(anchor: str = "def parse(text):") -> str:
    return json.dumps(
        {
            "report": {
                "problem": "Leere Texte stürzen ab",
                "cause": "parse prüft nichts",
                "evidence": [f"{PATH}:1: def parse(text):"],
                "fix": "parse lehnt leere Texte ab",
                "summary": "parse lehnt leere Texte ab",
                "steps": [{"path": PATH, "anchor": anchor, "do": "check the text"}],
                "test": {"path": "hub/tests/test_parse_empty.py", "do": "empty is refused"},
            }
        }
    )


class Asker:
    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, inputs: dict[str, Any]) -> tuple[str, str]:
        if name == "issue_area" and name not in self.answers:
            return "", "m-area"  # round 0 without an answer: the rounds go on as before
        self.calls.append((name, inputs))
        return self.answers[name].pop(0), f"m-{name}"


@pytest.mark.asyncio
async def test_the_lead_asks_dev_and_verwaltung_then_writes_report_and_plan() -> None:
    dev = {"to": "dev", "question": "Wo steht parse?"}
    admin = {"to": "admin", "question": "Welche Läufe brachen ab?"}
    questions = {"questions": [dev, admin]}
    asker = Asker(
        {
            "investigate_round": [json.dumps(questions), report()],
            "probe_dev": [f"{PATH}:1 def parse(text):"],
            "probe_admin": ["keine abgebrochenen Läufe"],
        }
    )
    inv = await ii.investigate(asker, read, "Leere Texte", "### start\ncode")
    assert inv.failed == "" and inv.plan is not None
    assert inv.plan["steps"][0]["anchor"] == "def parse(text):"
    assert [c[0] for c in asker.calls] == [
        "investigate_round", "probe_dev", "probe_admin", "investigate_round",
    ]  # fmt: skip
    # the second round reads what both answered
    findings = asker.calls[3][1]["findings"]
    assert "Dev: Wo steht parse?" in findings and "keine abgebrochenen Läufe" in findings
    text = ii.with_log(inv)
    for head in ("## Problem", "## Ursache", "## Belege", "## Vorgeschlagener Fix",
                 "## Lösungsplan", "## Untersuchung"):  # fmt: skip
        assert head in text
    assert "Runde 1, Verwaltung" in text


@pytest.mark.asyncio
async def test_a_question_asked_before_is_not_sent_again_and_answers_are_shown() -> None:
    """Live 2026-11-19: one question in four rounds, the same answer each time; the report showed
    the questions only."""
    same = json.dumps({"questions": [{"to": "dev", "question": "Wie liest der Filter Werte?"}]})
    asker = Asker({"investigate_round": [same, same, report()], "probe_dev": ["nur doppelte"]})
    inv = await ii.investigate(asker, read, "Frage", "")
    assert [c[0] for c in asker.calls].count("probe_dev") == 1
    assert "ASKED BEFORE" in asker.calls[3][1]["hint"]
    text = ii.with_log(inv)
    assert "## Fragen und Antworten" in text and "nur doppelte" in text


@pytest.mark.asyncio
async def test_a_plan_that_misses_the_code_is_another_round_not_the_persons() -> None:
    asker = Asker({"investigate_round": [report("def parse(raw_input):"), report()]})
    inv = await ii.investigate(asker, read, "Frage", "")
    assert inv.plan is not None and len(asker.calls) == 2
    assert "REFUSED BY THE HUB" in asker.calls[1][1]["hint"]
    assert "the file has: 'def parse(text):'" in asker.calls[1][1]["hint"]


QUESTIONS = (
    "Wo steht parse?",
    "Welche Tests prüfen den Leertext heute?",
    "Wer ruft die Funktion beim Import auf?",
    "Was liefert strip bei None zurück?",
    "Gibt es Logzeilen zu abgebrochenen Läufen?",
    "Welche Version von Python läuft im Container?",
)


def distinct(n: int) -> list[str]:
    """Questions of n rounds that are not alike (alike ones count as repeats)."""
    return [json.dumps({"questions": [{"to": "dev", "question": q}]}) for q in QUESTIONS[:n]]


@pytest.mark.asyncio
async def test_a_plan_refused_in_the_last_round_gets_repair_rounds() -> None:
    """Live 2026-11-21 (issue 2.10): the right places, but the one plan came in the last round
    with a paraphrased anchor and nothing was left to correct it."""
    rounds = [*distinct(ii.MAX_ROUNDS - 1), report("def parse(raw_input):"), report()]
    asker = Asker({"investigate_round": rounds, "probe_dev": ["?"] * ii.MAX_ROUNDS})
    inv = await ii.investigate(asker, read, "Frage", "")
    assert inv.plan is not None and inv.failed == ""
    repair = [c for c in asker.calls if c[0] == "investigate_round"][-1][1]["hint"]
    assert "REPAIR ROUND 1 OF" in repair and "the file has: 'def parse(text):'" in repair


@pytest.mark.asyncio
async def test_questions_in_the_last_round_are_not_answered_and_a_report_is_forced() -> None:
    """Live 2026-11-21 (issue 2.10): the last round asked again and the investigation ended with
    no report at all."""
    rounds = [*distinct(ii.MAX_ROUNDS), report()]
    asker = Asker({"investigate_round": rounds, "probe_dev": ["?"] * ii.MAX_ROUNDS})
    inv = await ii.investigate(asker, read, "Frage", "")
    assert [c[0] for c in asker.calls].count("probe_dev") == ii.MAX_ROUNDS - 1
    leads = [c for c in asker.calls if c[0] == "investigate_round"]
    assert len(leads) == ii.MAX_ROUNDS + 1 and "WRITE THE REPORT NOW" in leads[-1][1]["hint"]
    assert inv.plan is not None and "Fragen statt Bericht" in ii.with_log(inv)


@pytest.mark.asyncio
async def test_a_round_of_repeats_only_forces_the_report() -> None:
    """Live 2026-11-21 (issue 2.10): the lead asked for the code the fix is to write, four rounds
    long; nothing new could be found."""
    again = json.dumps({"questions": [{"to": "dev", "question": QUESTIONS[0]}]})
    asker = Asker({"investigate_round": [again, again, again, report()], "probe_dev": ["Zeile 1"]})
    inv = await ii.investigate(asker, read, "Frage", "")
    leads = [c for c in asker.calls if c[0] == "investigate_round"]
    assert "WRITE THE REPORT NOW" in leads[2][1]["hint"]  # after the round of repeats
    assert [c[0] for c in asker.calls].count("probe_dev") == 1 and inv.plan is not None


@pytest.mark.asyncio
async def test_the_rounds_end_and_the_last_one_is_told_so() -> None:
    # a plan that never fits: the repair rounds are used up, then the rounds end
    rounds = distinct(ii.MAX_ROUNDS - 1) + [report("nirgends")] * (1 + ii.MAX_REPAIRS)
    asker = Asker({"investigate_round": rounds, "probe_dev": ["?"] * ii.MAX_ROUNDS})
    inv = await ii.investigate(asker, read, "Frage", "")
    leads = [c for c in asker.calls if c[0] == "investigate_round"]
    assert len(leads) == ii.MAX_ROUNDS + ii.MAX_REPAIRS
    assert "LAST ROUND" in leads[ii.MAX_ROUNDS - 1][1]["hint"]
    assert f"REPAIR ROUND {ii.MAX_REPAIRS} OF {ii.MAX_REPAIRS}" in leads[-1][1]["hint"]
    # the report stands, its plan does not (the person sees why)
    assert inv.plan is None and "passt nicht zum Code" in inv.text and inv.failed == ""
    assert inv.draft is not None and inv.draft["steps"][0]["anchor"] == "nirgends"
    # no report at all, not even in the rounds that force one
    asker = Asker({"investigate_round": ["kein json"] * (ii.MAX_ROUNDS + ii.MAX_REPAIRS)})
    inv = await ii.investigate(asker, read, "Frage", "")
    assert inv.failed.startswith("nach") and "NEITHER" in asker.calls[1][1]["hint"]


@pytest.mark.asyncio
async def test_the_settled_plan_is_the_one_of_the_newest_result(client: TestClient) -> None:
    from topcoat_hub.project_events import PERSON
    from topcoat_hub.project_runs import RunService

    sm = client.app.state.sessionmaker
    store = ProjectStore(sm)
    project = await store.create_project(USER, title=f"Plan {uuid.uuid4().hex[:6]}")
    body = "Zielanwendung: dev\nErgebnisform: Diagnose\n"
    issue = await store.create_node(USER, project.id, kind="task", title="Leer", body=body)
    runs = RunService(sm, SimpleNamespace(), PERSON)
    assert await runs.issue_plan(USER, issue.id) is None
    plan = {"summary": "s", "steps": [], "test": {"path": "hub/tests/t.py", "do": "d"}}
    ids = []
    for program, diag in (("issue_investigate", {"plan": plan}), ("issue_plan", {})):
        tid = uuid.uuid4()
        ids.append(tid)
        async with sm() as db:
            diagnosis = {"program": program, "report": {"text": "x", **diag}}
            db.add(AgentTask(id=tid, user_id=USER, prompt="p", title="t", status="done",
                             diagnosis=diagnosis))  # fmt: skip
            await db.commit()
        await store.create_node(USER, project.id, kind="report", title="Ergebnis",
                                body=f"Bericht\n\n_Lauf: {tid}_", parent_id=issue.id,
                                report_kind="document")  # fmt: skip
        if program == "issue_investigate":
            assert await runs.issue_plan(USER, issue.id) == plan
    # the fix of the plan filed its own result: the plan is carried out, no button any more
    assert await runs.issue_plan(USER, issue.id) is None


@pytest.mark.asyncio
async def test_the_report_names_a_title_and_checks_for_the_issue() -> None:
    """2026-11-20, the maintainer: the person describes the problem; the agent finds out and
    writes what is checked and tested."""
    doc = json.loads(report())
    doc["report"]["title"] = "Leere Texte werden abgelehnt"
    doc["report"]["checks"] = ["Ein leerer Text gibt eine Fehlermeldung", "  ", 3]
    asker = Asker({"investigate_round": [json.dumps(doc)]})
    inv = await ii.investigate(asker, read, "Frage", "")
    assert inv.title == "Leere Texte werden abgelehnt"
    assert inv.checks == ["Ein leerer Text gibt eine Fehlermeldung"]
    assert "## Prüfpunkte\n- Ein leerer Text gibt eine Fehlermeldung" in inv.text


@pytest.mark.asyncio
async def test_the_same_question_in_other_words_is_not_sent_again() -> None:
    """Live 2026-11-21 (issue 2.10): one question reworded over four rounds."""
    first = "In which file and at what line(s) is the `collapsed` state initialized?"
    again = "In which file and at which line(s) is the `collapsed` state initialised?"
    rounds = [json.dumps({"questions": [{"to": "dev", "question": q}]}) for q in (first, again)]
    asker = Asker({"investigate_round": [*rounds, report()], "probe_dev": ["Zeile 95"]})
    await ii.investigate(asker, read, "Frage", "")
    assert [c[0] for c in asker.calls].count("probe_dev") == 1
    assert "ASKED BEFORE" in asker.calls[3][1]["hint"]


@pytest.mark.asyncio
async def test_the_next_investigation_starts_from_the_previous_report(client: TestClient) -> None:
    """Live 2026-11-21: a refused plan filed no result, so the next iteration of the issue asked
    everything again. The report of the issue's previous investigation is where the next starts."""
    from topcoat_hub.project_events import PERSON
    from topcoat_hub.project_runs import RunService

    sm = client.app.state.sessionmaker
    runs = RunService(sm, SimpleNamespace(), PERSON)
    node = uuid.uuid4()
    rows = (
        ("issue_investigate", str(node), "## Problem\nalt\n\n## Untersuchung\n- Runde 1"),
        ("issue_investigate", str(node), "**Untersuchung ohne Ergebnis.**\n\nnichts"),  # newest
        ("issue_investigate", str(uuid.uuid4()), "## Problem\nein anderes Issue"),
        ("issue_plan", str(node), "## Problem\nkein Bericht einer Untersuchung"),
    )
    draft = {"summary": "s", "steps": [{"path": "a.py", "anchor": "x", "do": "d"}]}
    for program, of, text in rows:
        async with sm() as db:
            db.add(AgentTask(id=uuid.uuid4(), user_id=USER, prompt="p", title="t", status="done",
                             diagnosis={"program": program, "node": of,
                                        "report": {"text": text, "draft": draft}}))  # fmt: skip
            await db.commit()
    previous = await runs._previous_investigation(USER, node, uuid.uuid4())
    # the newest one that wrote a report, without its log, with its refused plan as written
    assert previous == ("## Problem\nalt", draft)
    assert await runs._previous_investigation(USER, uuid.uuid4(), uuid.uuid4()) == ("", None)


@pytest.mark.asyncio
async def test_the_previous_refused_plan_is_checked_again_with_todays_rules(
    client: TestClient,
) -> None:
    """The plan of issue 2.10 (2026-11-21) was refused for a test file among its steps; the hub
    moves that now, so the next investigation is told the plan fits."""
    from topcoat_hub.project_events import PERSON
    from topcoat_hub.project_runs import RunService

    runs = RunService(client.app.state.sessionmaker, SimpleNamespace(), PERSON)

    async def read_live(user_id: Any, paths: list[str]) -> dict[str, str | None]:
        return await read(paths)

    runs.fixes = SimpleNamespace(read_live=read_live)
    draft = json.loads(report())["report"]
    draft["steps"].append({"path": "hub/tests/test_parse_empty.py", "anchor": "N/A", "do": "t"})
    note = await runs._draft_note(USER, {k: draft[k] for k in ("summary", "steps", "test")})
    assert "it FITS the code now" in note and '"anchor": "def parse(text):"' in note
    draft["steps"][0]["anchor"] = "def parse(raw_input):"
    note = await runs._draft_note(USER, {k: draft[k] for k in ("summary", "steps", "test")})
    assert "still wrong" in note and "the file has: 'def parse(text):'" in note


@pytest.mark.asyncio
async def test_the_second_look_refuses_a_plan_at_the_wrong_place_and_the_lead_repairs_it() -> None:
    """Live 2026-11-21 (issue 2.10): the anchors stood in the code, but the change went into the
    filter search instead of the opening of a project. A plan that fits the code is read by
    another model with the code around its anchors before it is taken."""
    no = json.dumps({"ok": False, "problems": ["step 1: parse is the wrong place"]})
    asker = Asker(
        {
            "investigate_round": [report(), report()],
            "review_plan": [no, json.dumps({"ok": True})],
        }
    )
    inv = await ii.investigate(asker, read, "Frage", "", review=True)
    assert inv.plan is not None
    reviews = [c[1] for c in asker.calls if c[0] == "review_plan"]
    assert len(reviews) == 2 and "1: def parse(text):" in reviews[0]["code"]
    assert (
        "the wrong place" in [c for c in asker.calls if c[0] == "investigate_round"][1][1]["hint"]
    )
    log = ii.with_log(inv)
    assert "Zweitcheck (m-review_plan) lehnt den Plan ab" in log and "Plan passt" in log


@pytest.mark.asyncio
async def test_an_unclear_second_look_does_not_block_and_reviews_are_limited() -> None:
    asker = Asker({"investigate_round": [report()], "review_plan": ["kein json"]})
    inv = await ii.investigate(asker, read, "Frage", "", review=True)
    assert inv.plan is not None and "keine klare Antwort" in ii.with_log(inv)
    # two refusals, then a plan that fits the code is taken without a third look
    no = json.dumps({"ok": False, "problems": ["falsch"]})
    asker = Asker({"investigate_round": [report()] * 3, "review_plan": [no, no]})
    inv = await ii.investigate(asker, read, "Frage", "", review=True)
    assert inv.plan is not None
    assert [c[0] for c in asker.calls].count("review_plan") == ii.MAX_REVIEWS


def test_the_wiki_paragraphs_that_name_the_issues_files_come_first() -> None:
    """Live 2026-11-21 (issue 2.10): the wiki page of the Projekte GUI said what the planner got
    wrong nine times, and no run of the investigation read it."""
    pages = [
        ("B Hub/B12 Wissensbasis", "Die Wissensbasis liest Dateien.\n\nNichts zur Oberfläche."),
        (
            "D Oberfläche/D3a Projekte-Oberfläche",
            "In `ProjectPage.tsx` enthält `visible` die durch `visibleNodes` "
            "berechneten Knoten.\n\n"
            "Das Logo ist blau.\n\n"
            "`found` hält die Antwort einer Filtersuche; `listRows` kommt aus `visible`.",
        ),
    ]
    text = (
        "Die Baum-Ansicht: `collapsed` in web/src/project/ProjectPage.tsx, "
        "`found` und `visibleNodes`."
    )
    out = ii.wiki_excerpt(pages, text)
    assert out.startswith("### D Oberfläche/D3a Projekte-Oberfläche\n")
    assert "visibleNodes" in out and "Filtersuche" in out
    assert "Logo" not in out and "Wissensbasis" not in out
    assert ii.wiki_excerpt(pages, "nichts Bekanntes") == ""
    assert {"ProjectPage.tsx", "ProjectPage", "visibleNodes", "found"} <= ii.wiki_terms(text)


@pytest.mark.asyncio
async def test_the_person_gives_the_plan_and_plan_umsetzen_carries_it(client: TestClient) -> None:
    """2026-11-21 ("Plan vorgeben"): the local planners found the places of issue 2.10 but planned
    the new GUI code wrong ten times; the person writes the plan, the hub checks and files it."""
    from tests.conftest import AUTH

    sm = client.app.state.sessionmaker
    store = ProjectStore(sm)
    project = await store.create_project(USER, title=f"Gegeben {uuid.uuid4().hex[:6]}")
    body = "Zielanwendung: dev\nProgramm: issue_investigate\nErgebnisform: Diagnose\nArt: issue\n"
    issue = await store.create_node(USER, project.id, kind="task", title="Leer", body=body)
    plain = await store.create_node(USER, project.id, kind="task", title="Kein Issue", body="x")
    runs = client.app.state.project_runs

    async def read_live(user_id: Any, paths: list[str]) -> dict[str, str | None]:
        return await read(paths)

    runs.fixes = SimpleNamespace(read_live=read_live)
    given = {
        "summary": "parse lehnt leere Texte ab",
        "steps": [{"path": PATH, "anchor": "def parse(texts):", "do": "check the text"}],
        "test": {"path": "hub/tests/test_parse_empty.py", "do": "empty is refused"},
    }
    r = client.post(f"/api/project/nodes/{issue.id}/plan", json=given, headers=AUTH)
    assert r.status_code == 201, r.text
    # the close anchor was settled to the line of the file
    assert r.json()["plan"]["steps"][0]["anchor"] == "def parse(text):"
    assert (await runs.issue_plan(USER, issue.id))["summary"] == "parse lehnt leere Texte ab"
    node, _ = await runs._store.load(USER, issue.id)
    assert node.status == "in_review"
    # a plan that does not fit the code is refused with the reason, nothing is filed
    wrong = {**given, "steps": [{"path": PATH, "anchor": "def nothing_like_it(x, y):", "do": "x"}]}
    r = client.post(f"/api/project/nodes/{issue.id}/plan", json=wrong, headers=AUTH)
    assert r.status_code == 422 and "is not in the file" in r.json()["detail"]["message"]
    r = client.post(f"/api/project/nodes/{plain.id}/plan", json=given, headers=AUTH)
    assert r.status_code == 422 and "only an issue" in r.json()["detail"]["message"]


def test_an_issue_without_a_file_name_reads_the_wiki_page_of_its_window() -> None:
    """Live 2026-11-21 (issues 2.12, 2.13): "die Baum-Ansicht" with no file named; the planner took
    the tag tree of the memory and invented files. The issue says which window it came from."""
    pages = [
        ("B Hub/B5 Gedächtnis", "Der Tag-Baum zeigt die Tags.\n\nTagTree rendert Zeilen."),
        (
            "D Oberfläche und Desktop/D3a Projekte-Oberfläche: Seite und Knoten",
            "`ListView` rendert die Zeilen des Baums.\n\n`visibleNodes` baut sie.",
        ),
    ]
    text = (
        "In der Baum-Ansicht ist es unübersichtlich.\n\n"
        "## Kontext (vom Hub)\n- Fenster: project (Gespräch x)"
    )
    out = ii.wiki_excerpt(pages, text)
    assert out.startswith("### D Oberfläche und Desktop/D3a Projekte-Oberfläche")
    assert "ListView" in out and "visibleNodes" in out and "Tag-Baum" not in out
    assert ii.window_page("- Fenster: chat") == "Chat-Oberfläche"
    assert ii.window_page("kein Fenster") is None


# --- round 0: the area (plan step 1.15) -----------------------------------------------------------

TAURI = "desktop/src-tauri/src/main.rs"


def area(kind: str, files: list[str]) -> str:
    return json.dumps({"area": kind, "files": files, "symbols": ["clip"], "why": "w"})


async def read_with_desktop(paths: list[str]) -> dict[str, str | None]:
    return {p: ("fn main() {}\n" if p == TAURI else LIVE.get(p)) for p in paths}


@pytest.mark.asyncio
async def test_round_0_names_the_area_and_the_rounds_read_it_first() -> None:
    asker = Asker({"issue_area": [area("hub", [PATH])], "investigate_round": [report()]})
    inv = await ii.investigate(asker, read, "Leere Texte", "### start")
    assert inv.failed == "" and inv.plan is not None
    findings = asker.calls[1][1]["findings"]
    assert "## Runde 0: Bereich" in findings and f"`{PATH}`" in findings
    assert inv.log[0] == f"Runde 0 (m-issue_area): Bereich hub: {PATH}"


@pytest.mark.asyncio
async def test_an_issue_of_the_desktop_shell_stops_with_claude_code() -> None:
    asker = Asker({"issue_area": [area("desktop", [TAURI])], "investigate_round": []})
    inv = await ii.investigate(asker, read_with_desktop, "Clipper hängt", "### start")
    assert "desktop/" in inv.failed and "Claude Code" in inv.failed
    assert inv.plan is None and "außerhalb der Fix-Pipeline" in inv.text
    assert [c[0] for c in asker.calls] == ["issue_area"]  # no round asked anything


@pytest.mark.asyncio
async def test_a_guess_of_the_desktop_without_proof_stops_nothing() -> None:
    """Live 2026-11-23 (3.2.3.2): the place was in the hub, the model searched desktop/. A file
    of the pipeline among the named ones, or a desktop file that does not exist, goes on."""
    for files in ([TAURI, PATH], ["desktop/nowhere.rs"]):
        asker = Asker({"issue_area": [area("desktop", files)], "investigate_round": [report()]})
        inv = await ii.investigate(asker, read_with_desktop, "Clipper hängt", "### start")
        assert inv.failed == "" and inv.plan is not None, files


@pytest.mark.asyncio
async def test_a_plan_only_in_the_desktop_shell_ends_without_repair_rounds() -> None:
    plan = json.loads(report())
    plan["report"]["steps"] = [{"path": TAURI, "symbol": "main", "do": "x"}]
    asker = Asker({"investigate_round": [json.dumps(plan)]})
    inv = await ii.investigate(asker, read_with_desktop, "Clipper hängt", "### start")
    assert "Claude Code" in inv.failed and [c[0] for c in asker.calls] == ["investigate_round"]


@pytest.mark.asyncio
async def test_a_desktop_guess_against_hub_hits_is_dropped() -> None:
    """Live 2026-11-25 (3.2.3.2, "Sheets im Chat"): gpt-oss named desktop/…/overlay.rs, a file
    that exists, and the investigation stopped; the search at the start had found the hub's
    files. Those hits decide: the guess is dropped and not given to the rounds."""
    start = "### hub/src/topcoat_hub/chat_tools.py (lines 1-40)\ndef decider_tool(...)"
    asker = Asker({"issue_area": [area("desktop", [TAURI])], "investigate_round": [report()]})
    inv = await ii.investigate(asker, read_with_desktop, "Sheets im Chat", start)
    assert inv.failed == "" and inv.plan is not None
    assert "verworfen" in inv.log[0] and "hub/src/topcoat_hub/chat_tools.py" in inv.log[0]
    assert "## Runde 0" not in asker.calls[1][1]["findings"]
    assert ii.inside_paths("a hub/x.py b web/src/a/B.tsx desktop/c.rs hub/x.py") == [
        "hub/x.py", "web/src/a/B.tsx",
    ]  # fmt: skip


# --- the map of the files found (repo.outline, plan step 1.11) ------------------------------------

WEB = "web/src/project/ListView.tsx"
# what a dev node's repo.outline answers for a file of the fixture repository
OUTLINE: dict[str, list[dict[str, Any]]] = {
    PATH: [
        {
            "path": PATH,
            "kind": "py",
            "lines": 2,
            "symbols": [{"name": "parse", "kind": "function", "start": 1, "end": 2}],
        }
    ],
    WEB: [
        {
            "path": WEB,
            "kind": "ts",
            "lines": 120,
            "symbols": [
                {"name": "ListView", "kind": "component", "start": 10, "end": 118},
                {"name": "ListView.visibleNodes", "kind": "const", "start": 14, "end": 30},
            ],
            "symbols_left": 7,
        }
    ],
}


class FakeOutline:
    def __init__(self, fail: bool = False) -> None:
        self.asked: list[str] = []
        self.fail = fail

    async def __call__(self, path: str) -> list[dict[str, Any]]:
        self.asked.append(path)
        if self.fail:
            raise RuntimeError("capability repo.outline is not offered by this node version")
        return OUTLINE.get(path, [])


@pytest.mark.asyncio
async def test_round_0_reads_the_map_of_the_files_the_start_found() -> None:
    start = f"### {WEB}:14: const visibleNodes\n### {PATH}:1: def parse\n{WEB}:40: found(x)"
    outline = FakeOutline()
    asker = Asker({"issue_area": [area("web", [WEB])], "investigate_round": [report()]})
    inv = await ii.investigate(asker, read, "Leere Texte", start, outline=outline)
    assert inv.failed == "" and inv.plan is not None
    assert outline.asked == [WEB, PATH]  # the most named first
    area_findings = asker.calls[0][1]["findings"]
    assert "## Übersicht der gefundenen Dateien" in area_findings
    assert (
        f"- `{WEB}` (120 Zeilen): ListView [10-118], ListView.visibleNodes [14-30], … 7 weitere"
        in (area_findings)
    )
    assert f"- `{PATH}` (2 Zeilen): parse [1-2]" in area_findings
    # the rounds after round 0 keep the map
    assert "ListView.visibleNodes [14-30]" in asker.calls[1][1]["findings"]
    assert inv.log[0] == "Runde 0: Übersicht der gefundenen Dateien gelesen"


@pytest.mark.asyncio
async def test_without_the_capability_or_files_the_investigation_goes_on_without_a_map() -> None:
    start = f"### {PATH}:1: def parse"
    for outline, begin in ((FakeOutline(fail=True), start), (FakeOutline(), "### nichts gefunden")):
        asker = Asker({"issue_area": [area("hub", [PATH])], "investigate_round": [report()]})
        inv = await ii.investigate(asker, read, "Leere Texte", begin, outline=outline)
        assert inv.failed == "" and inv.plan is not None
        assert "Übersicht" not in asker.calls[0][1]["findings"]
        assert not inv.log[0].startswith("Runde 0: Übersicht")


def test_the_map_text_is_cut_and_names_the_files_in_order() -> None:
    one = {"name": "f", "start": 1, "end": 9}
    files = [{"path": f"hub/m{i}.py", "lines": 9, "symbols": [one]} for i in range(50)]
    text = ii.outline_text(files, limit=200)
    assert text.endswith("- …") and len(text) < 260
    assert (
        ii.outline_text([{"path": "docs/A.md", "lines": 0, "symbols": []}])
        == "- `docs/A.md` (0 Zeilen): -"
    )
    assert ii.named_files("a node/x.go b hub/y.py hub/y.py c hub/y.py:3 web/src/z.tsx", 2) == [
        "hub/y.py", "node/x.go",
    ]  # fmt: skip
