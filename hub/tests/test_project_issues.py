"""Issues from the running system (project_issues.py, 2026-11-17): "Problem: …" in a chat files a
task below "Issues" with the hub's context; a complaint in Projekte gets a card instead."""

import logging
import uuid
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from topcoat_hub import hub_log
from topcoat_hub import project_issues as pi
from topcoat_hub.models import DEV_ADMIN_ID as USER
from topcoat_hub.project_store import ProjectStore


def test_the_prefix_files_and_a_complaint_only_sounds_like_one() -> None:
    assert pi.filed_text("Problem: Fenster springt beim Ziehen") == "Fenster springt beim Ziehen"
    assert pi.filed_text("/issue Clipper hängt") == "Clipper hängt"
    assert pi.filed_text("bug:   x") == "x"
    assert pi.filed_text("Das Problem ist …") is None
    assert pi.filed_text("Problem:") is None
    assert pi.sounds_like_problem("Der Export geht nicht mehr")


def test_an_issue_asked_for_in_words_is_filed_with_the_rest():
    # live 2026-11-20: the project agent made a loose task at the root from this message
    asked = (
        "Bitte füge einen neuen Issue hinzu. Ich finde die Baum-Ansicht in Projekte gerade sehr "
        "unübersichtlich. 2.1 z.B. hat 5 unterpunkte, welche als Default alle angezeigt werden."
    )
    rest = pi.asked_text(asked)
    assert rest is not None and rest.startswith("Ich finde die Baum-Ansicht")
    assert pi.title_of(rest) == "Ich finde die Baum-Ansicht in Projekte gerade sehr unübersichtlich"
    assert (
        pi.asked_text("Leg bitte ein Issue an: Clipper hängt beim Ziehen")
        == "Clipper hängt beim Ziehen"
    )
    assert pi.asked_text("Please file a bug. The export is empty.") == "The export is empty."
    # an ask without the problem, a question about issues, a task without the word: not filed
    assert pi.asked_text("Bitte füge einen neuen Issue hinzu.") is None
    assert pi.asked_text("Wie viele Issues sind offen? Und welche davon alt?") is None
    assert pi.asked_text("Lege eine Aufgabe an. Sie heißt Rollout.") is None
    assert not pi.sounds_like_problem("Wie lege ich ein Projekt an?")


def test_titles_are_short_and_similar_issues_are_named() -> None:
    long = "Wenn ich ein Fenster greife und verschiebe, bleibt es stehen " * 4
    assert len(pi.title_of(long)) <= pi.FIRST_TITLE and pi.title_of(long).endswith("…")
    other = SimpleNamespace(title="Fenster bleibt beim Verschieben stehen")
    unrelated = SimpleNamespace(title="Export der Akte schlägt fehl")
    assert pi.similar("Fenster bleibt stehen beim Verschieben", [other, unrelated]) == [other]  # type: ignore[list-item]


def test_the_log_buffer_keeps_warnings_and_masks_secrets() -> None:
    handler = hub_log.RingHandler(size=3)
    log = logging.getLogger("test.issue")
    log.addHandler(handler)
    try:
        log.info("not kept")
        log.warning("token=abc123 failed for %s", "node-nvidia")
    finally:
        log.removeHandler(handler)
    [line] = handler.recent()
    assert "abc123" not in line.text and "token=***" in line.text and "node-nvidia" in line.text


async def _project(client: TestClient) -> Any:
    store = ProjectStore(client.app.state.sessionmaker)
    for p in await store.list_projects(USER):
        if p.title == "Topcoat":
            return store, p
    return store, await store.create_project(USER, title="Topcoat")


def test_problem_in_a_chat_files_an_issue_with_context(client: TestClient) -> None:
    from tests.test_chat import new_conv
    from tests.test_chat_tools import ask_with

    store, project = client.portal.call(lambda: _project(client))
    logging.getLogger("topcoat_hub.test").warning("drag failed in Desktop %s", uuid.uuid4().hex[:6])
    conv = new_conv(client, application="chat")
    status, events, _ = ask_with(client, conv, "Problem: Fenster springt beim Ziehen zurück")
    kinds = [e for e, _ in events]
    assert status == 200 and kinds[-1] == "done", kinds
    card = next(d for e, d in events if e == "report")["message"]["report"]["issue"]
    assert card["filed"] and card["title"] == "Fenster springt beim Ziehen zurück"
    task = client.portal.call(lambda: store.get_node(USER, uuid.UUID(card["task_id"])))
    assert pi.is_issue(task) and "Programm: issue_investigate" in task.body
    assert "## Kontext (vom Hub)" in task.body and "Build:" in task.body
    assert "drag failed in Desktop" in task.body  # the hub's own warning of the moment
    nodes = client.portal.call(lambda: store.tree(USER, project.id))
    parent = next(n for n in nodes if n.id == task.parent_id)
    assert parent.kind == "topic" and parent.title == "Issues"
    labels = client.portal.call(lambda: store.labels_of(USER, task.id))
    assert [x.title for x in labels] == ["issue"]
    done = next(d for e, d in events if e == "done")["message"]["content"]
    assert f"Issue {task.number}" in done


def test_a_complaint_in_projekte_is_a_card_and_files_nothing(client: TestClient) -> None:
    from tests.test_chat import new_conv
    from tests.test_chat_tools import ask_with

    store, project = client.portal.call(lambda: _project(client))
    before = len(client.portal.call(lambda: store.tree(USER, project.id)))
    conv = new_conv(client, application="project")
    status, events, _ = ask_with(client, conv, "Der Export der Akte geht nicht mehr")
    card = next(d for e, d in events if e == "report")["message"]["report"]["issue"]
    assert status == 200 and card["filed"] is False and "Export" in card["text"]
    assert len(client.portal.call(lambda: store.tree(USER, project.id))) == before
    r = client.post(
        "/api/project/issues",
        json={"text": card["text"], "application": "project", "conversation": card["conversation"]},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    assert r.json()["task"]["title"] == "Der Export der Akte geht nicht mehr"


def test_check_open_runs_the_issues_without_a_result(client: TestClient) -> None:
    store, project = client.portal.call(lambda: _project(client))

    async def setup() -> Any:
        a, _ = await pi.file_issue(store, USER, "Problem A tritt auf", project_title="Topcoat")
        b, _ = await pi.file_issue(store, USER, "Problem B tritt auf", project_title="Topcoat")
        await store.create_node(
            USER, project.id, kind="report", report_kind="document", title="Ergebnis", body="x",
            parent_id=b.id,
        )  # fmt: skip
        return a, b

    a, b = client.portal.call(setup)
    nodes = client.portal.call(lambda: store.tree(USER, project.id))
    chosen = {n.id for n in pi.select_open(nodes)}
    assert a.id in chosen and b.id not in chosen


def test_the_first_title_ends_at_a_clause_not_at_an_abbreviation():
    # live 2026-11-20: issue 2.10 was titled "... ein Punkt mit vielen Unterpunkten (z.B."
    text = (
        "Die Baum-Ansicht in Projekte ist unübersichtlich: ein Punkt mit vielen Unterpunkten "
        "(z.B. 2.1 mit 5 Berichten) ist immer aufgeklappt.\nGewünscht: …"
    )
    assert pi.title_of(text) == "Die Baum-Ansicht in Projekte ist unübersichtlich"
    assert pi.title_of("Clipper hängt") == "Clipper hängt"
    # a short first line is the title as it is (1.15): no cut at its first sentence or colon
    assert pi.title_of("Wo ist z.B. der Export geblieben? Er fehlt.") == (
        "Wo ist z.B. der Export geblieben? Er fehlt"
    )
    assert pi.title_of("Wiki: Seiten veralten\nmehr") == "Wiki: Seiten veralten"


BODY = (
    "Zielanwendung: dev\nProgramm: issue_investigate\nErgebnisform: Diagnose\nArt: issue\n"
    "Gemeldet: 2026-11-20 23:19 (Chat project)\n\n"
    "Die Baum-Ansicht ist unübersichtlich: alles aufgeklappt.\n\n"
    "## Kontext (vom Hub)\n- Build: x"
)


def test_the_investigation_names_the_issue_and_writes_its_checks():
    title = pi.title_of(pi.reported_text(BODY))
    assert title == "Die Baum-Ansicht ist unübersichtlich: alles aufgeklappt"
    new_title, body = pi.apply_brief(title, BODY, "Baum: große Punkte eingeklappt", ["A", "B"])
    assert new_title == "Baum: große Punkte eingeklappt"
    assert body is not None
    assert body.index("## Prüfpunkte (von der Untersuchung)\n- [ ] A\n- [ ] B") < body.index(
        "## Kontext (vom Hub)"
    )
    assert pi.reported_text(body) == "Die Baum-Ansicht ist unübersichtlich: alles aufgeklappt."
    # a second investigation replaces the checks, it does not add a second list
    _t, again = pi.apply_brief(new_title, body, "Anders", ["C"])
    assert again is not None and again.count("## Prüfpunkte") == 1 and "- [ ] C" in again
    assert "- [ ] A" not in again
    # a title the person gave stays
    kept, _b = pi.apply_brief("Mein eigener Titel", BODY, "Neu", ["A"])
    assert kept is None
