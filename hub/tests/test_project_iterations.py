"""Iterations of a task (project_iterations.py, 2026-11-17).

Live 2026-11-16, task 1.8.2: four versions of a theme, each written from scratch (the run never saw
the version before), and the feedback "dark" was lost when the chat rewrote the task text (v4 came
out light again). Now: a result is iteration N with what it continued and the feedback it answers;
the task keeps requirements; the next run gets the version before, the feedback and the
requirements."""

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import AUTH
from tests.test_project_runs import FakeAgents, FakeLook, _set_finish
from topcoat_hub.models import DEV_ADMIN_ID as USER
from topcoat_hub.models import ProjectNode
from topcoat_hub.project_base import Invalid
from topcoat_hub.project_iterations import (
    IterationStore,
    base_text,
    brief,
    clean_requirements,
    next_number,
    theme_in,
)
from topcoat_hub.project_store import ProjectStore

FORM_BODY = "Zielanwendung: dev\nProgramm: frei\nErgebnisform: Artikel | Befund\n- [ ] first\n"

THEME = {
    "key": "wire",
    "name": "Wire",
    "base": "terminal",
    "layout": {"kind": "cascade"},
    "bar": {"position": "top", "items": ["programs"]},
    "chrome": {"buttons": ["close"], "titlebar": "top"},
    "motion": {"style": "fade", "ms": 200},
    "css": "--bg: #000;",
}


# --- pure -----------------------------------------------------------------------------------------


def test_requirements_are_cleaned_deduplicated_and_kept_in_order() -> None:
    out = clean_requirements(
        ["  Dunkle   Grundfarbe ", "dunkle grundfarbe", "", {"text": "Kein Wackeln", "from": 2}]
    )
    assert [r["text"] for r in out] == ["Dunkle Grundfarbe", "Kein Wackeln"]
    assert out[1]["from"] == 2 and all(r["id"] for r in out)
    kept = clean_requirements(out)  # ids survive a round trip
    assert [r["id"] for r in kept] == [r["id"] for r in out]
    with pytest.raises(Invalid):
        clean_requirements([f"r{i}" for i in range(31)])


def test_the_theme_of_a_result_is_found_and_continued_alone() -> None:
    body = (
        "**Stand:** report written\n\n```json\n" + json.dumps(THEME) + "\n```\n\n"
        "## Vorschau\n\n![bild](x)\n\n_Lauf: 0f0f_"
    )
    assert theme_in(body) == THEME
    text = base_text(SimpleNamespace(body=body))  # type: ignore[arg-type]
    assert '"key": "wire"' in text and "Vorschau" not in text and "Stand" not in text


def test_the_brief_names_the_version_the_feedback_and_every_requirement() -> None:
    base = SimpleNamespace(
        iteration=3, number="1.8.2.3", body="```json\n" + json.dumps(THEME) + "\n```"
    )
    text = brief(
        4, base, "Fenster wackeln", [{"id": "a", "text": "dunkel"}, {"id": "b", "text": "ruhig"}]
    )  # type: ignore[arg-type]
    assert "# Iteration 4" in text and "iteration 3, 1.8.2.3" in text
    assert "Change only what" in text and '"key": "wire"' in text
    assert "Fenster wackeln" in text and "- dunkel" in text and "- ruhig" in text
    assert brief(1, None, "", []) == ""


def test_a_result_backed_by_reads_is_written_anew_not_copied() -> None:
    # live 2026-11-17: told to "keep everything else as it is", a survey copied its refused table
    base = SimpleNamespace(
        iteration=2,
        number="3.1.2.2",
        body=(
            "**Stand:** blockiert: row 1 not supported\n\n"
            "| Ergebnis | Beleg |\n|---|---|\n| x | E1 |"
        ),
    )
    text = brief(3, base, "Belege mit Datei:Zeile", [], evidence=True)  # type: ignore[arg-type]
    assert "not shown here on purpose" in text and "Change only what" not in text
    assert "| x | E1 |" not in text  # the refused table is no template
    assert "row 1 not supported" in text  # the reason it was refused is shown
    assert "Belege mit Datei:Zeile" in text


def test_numbers_follow_the_highest_iteration() -> None:
    rows = [
        SimpleNamespace(iteration=1),
        SimpleNamespace(iteration=None),
        SimpleNamespace(iteration=4),
    ]
    assert next_number(rows) == 5  # type: ignore[arg-type]
    assert next_number([]) == 1


# --- with the hub ---------------------------------------------------------------------------------


async def _task_with_results(
    client: TestClient, results: int
) -> tuple[ProjectStore, uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"Iter {uuid.uuid4().hex[:10]}")
    task = await store.create_node(
        USER, project.id, kind="task", title="Artikel prüfen", body=FORM_BODY
    )
    ids = []
    for i in range(results):
        report = await store.create_node(
            USER,
            project.id,
            kind="report",
            report_kind="document",
            title="Ergebnis: Theme",
            body=f"version {i + 1}",
            parent_id=task.id,
        )
        await IterationStore(client.app.state.sessionmaker).file(USER, task.id, report.id)
        ids.append(report.id)
    return store, project.id, task.id, ids


async def _get(client: TestClient, node_id: uuid.UUID) -> ProjectNode:
    async with client.app.state.sessionmaker() as db:
        return await db.scalar(select(ProjectNode).where(ProjectNode.id == node_id))


@pytest.mark.asyncio
async def test_filed_results_are_numbered_iterations(client: TestClient) -> None:
    _, _, _task, ids = await _task_with_results(client, 2)
    assert [(await _get(client, i)).iteration for i in ids] == [1, 2]


@pytest.mark.asyncio
async def test_a_new_iteration_runs_on_the_version_before_with_feedback_and_requirements(
    client: TestClient,
) -> None:
    runs = client.app.state.project_runs
    fake = FakeAgents(client)
    runs._agents = fake
    runs.second_look = FakeLook()
    _, _, task, (v1, _v2) = await _task_with_results(client, 2)
    node = await _get(client, task)
    await IterationStore(client.app.state.sessionmaker).set_requirements(
        USER, task, ["dunkle Grundfarbe"], revision=node.revision
    )
    r = client.post(
        f"/api/project/nodes/{task}/iterate",
        json={"based_on": str(v1), "feedback": "Kein Dauerwackeln", "keep": True},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    prompt = fake.started[0]["prompt"]
    # the version it builds on is named (its text is not shown to a result backed by reads)
    assert "# Iteration 3" in prompt and "(iteration 1," in prompt and "(iteration 2," not in prompt
    assert "Kein Dauerwackeln" in prompt and "- dunkle Grundfarbe" in prompt
    node = await _get(client, task)
    assert node.status == "in_progress"
    assert [x["text"] for x in node.requirements] == ["dunkle Grundfarbe", "Kein Dauerwackeln"]
    assert node.requirements[1]["from"] == 3
    # the run ends: its result is iteration 3, built on 1, answering the feedback
    await _set_finish(client, fake.started[0]["id"], [{"Artikel": "a", "Befund": "ok"}], "done")
    await runs.on_task_event(USER, fake.started[0]["id"], "done")
    async with client.app.state.sessionmaker() as db:
        newest = await db.scalar(
            select(ProjectNode)
            .where(ProjectNode.parent_id == task, ProjectNode.kind == "report")
            .order_by(ProjectNode.created_at.desc())
        )
    assert newest.iteration == 3 and newest.based_on_id == v1
    assert newest.feedback == "Kein Dauerwackeln"
    assert (await _get(client, task)).next_iteration is None
    listed = client.get(f"/api/project/nodes/{task}/iterations", headers=AUTH).json()
    assert [i["iteration"] for i in listed["iterations"]] == [1, 2, 3]
    assert listed["iterations"][2]["based_on"] == (await _get(client, v1)).number


@pytest.mark.asyncio
async def test_a_version_of_another_task_cannot_be_continued(client: TestClient) -> None:
    _, _, task, _ = await _task_with_results(client, 1)
    _, _, _, (other,) = await _task_with_results(client, 1)
    r = client.post(
        f"/api/project/nodes/{task}/iterate", json={"based_on": str(other)}, headers=AUTH
    )
    assert r.status_code == 422
    assert (await _get(client, task)).next_iteration is None


@pytest.mark.asyncio
async def test_requirements_are_set_by_the_person_with_the_revision(client: TestClient) -> None:
    _, _, task, _ = await _task_with_results(client, 0)
    node = await _get(client, task)
    r = client.put(
        f"/api/project/nodes/{task}/requirements",
        json={"revision": node.revision, "items": ["schwarz", "ruhig"]},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    assert [x["text"] for x in r.json()["requirements"]] == ["schwarz", "ruhig"]
    stale = client.put(
        f"/api/project/nodes/{task}/requirements",
        json={"revision": node.revision, "items": []},
        headers=AUTH,
    )
    assert stale.status_code == 409


@pytest.mark.asyncio
async def test_a_failed_start_leaves_no_iteration_waiting(client: TestClient) -> None:
    _, _, task, _ = await _task_with_results(client, 1)
    runs = client.app.state.project_runs
    fake: Any = FakeAgents(client)
    runs._agents = fake
    async with client.app.state.sessionmaker() as db:  # no result form: the run is refused
        node = await db.get(ProjectNode, task)
        node.body = "Zielanwendung: dev\n"
        await db.commit()
    r = client.post(f"/api/project/nodes/{task}/iterate", json={"feedback": "x"}, headers=AUTH)
    assert r.status_code == 409
    assert (await _get(client, task)).next_iteration is None


@pytest.mark.asyncio
async def test_full_places_answer_429_and_leave_no_iteration_open(client: TestClient) -> None:
    """Issue 2.16 (live 2026-11-21): with every place of the person's programs taken, iterate
    answered HTTP 500 and the begun iteration stayed open."""
    from topcoat_hub.project_runs import ProgramsBusy

    _, _, task, _ = await _task_with_results(client, 1)
    runs = client.app.state.project_runs

    async def busy(*_args: Any, **_kwargs: Any) -> Any:
        raise ProgramsBusy("too many runs are going")

    original = runs.start_node
    runs.start_node = busy
    try:
        r = client.post(f"/api/project/nodes/{task}/iterate", json={"feedback": "x"}, headers=AUTH)
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "busy"
        assert int(r.headers["retry-after"]) > 0
        assert (await _get(client, task)).next_iteration is None
        assert client.post(f"/api/project/nodes/{task}/run", headers=AUTH).status_code == 429
    finally:
        runs.start_node = original


# --- the Projekte chat: "make the next version" ---------------------------------------------------


def test_a_request_for_a_new_version_is_recognised() -> None:
    from topcoat_hub.project_iterations import wants_iteration

    assert wants_iteration("Bitte aktualisiere 1.8.2 auf einen v4 Lauf")
    assert wants_iteration("Mach eine neue Version von 1.8.2, dunkler")
    assert wants_iteration("next iteration of 1.8.2 please")
    assert not wants_iteration("Bitte formuliere 1.8.1 und 1.8.2 aus")


def test_a_number_of_a_result_names_its_task_and_that_version() -> None:
    from topcoat_hub.project_iterations import iteration_target

    task = SimpleNamespace(
        id=uuid.uuid4(), number="1.8.2", kind="task", parent_id=None, trashed_at=None
    )
    other = SimpleNamespace(
        id=uuid.uuid4(), number="1.8.1", kind="task", parent_id=None, trashed_at=None
    )

    def result(n: int) -> Any:
        return SimpleNamespace(
            id=uuid.uuid4(), number=f"1.8.2.{n}", kind="report", parent_id=task.id,
            trashed_at=None, iteration=n, created_at=None,
        )  # fmt: skip

    r1, r2 = result(1), result(2)
    nodes = [task, other, r1, r2]
    assert iteration_target(nodes, ["1.8.2.1"]) == (task, r1)  # type: ignore[arg-type]
    assert iteration_target(nodes, ["1.8.2"]) == (task, r2)  # type: ignore[arg-type]
    assert iteration_target(nodes, ["1.8.1"]) is None  # type: ignore[arg-type]


def test_in_projekte_a_new_version_is_a_card_and_the_task_stays(
    client: TestClient, connect: Any
) -> None:
    from tests.fakenode import online_node
    from tests.test_chat import new_conv, use_model
    from tests.test_chat_tools import ask_with

    online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    store = ProjectStore(client.app.state.sessionmaker)
    title = f"Wire{uuid.uuid4().hex[:6]}"

    async def setup() -> Any:
        project = await store.create_project(USER, title=title)
        task = await store.create_node(USER, project.id, kind="task", title="Theme", body=FORM_BODY)
        for i in (1, 2):
            report = await store.create_node(
                USER, project.id, kind="report", report_kind="document",
                title="Ergebnis: Theme", body=f"v{i}", parent_id=task.id,
            )  # fmt: skip
            await IterationStore(client.app.state.sessionmaker).file(USER, task.id, report.id)
        return task

    task = client.portal.call(setup)
    conv = new_conv(client, application="project")
    text = f"Bitte mach in {title} eine v3 aus 1.2, der Hintergrund soll schwarz sein"
    status, events, _ = ask_with(client, conv, text)
    kinds = [e for e, _ in events]
    assert status == 200 and kinds[-1] == "done", kinds
    assert "tools_start" not in kinds
    offer = next(d for e, d in events if e == "report")["message"]["report"]["iterate"]
    assert offer["task_id"] == str(task.id) and offer["iteration"] == 3
    assert offer["based_on_number"] == "1.2" and "schwarz" in offer["feedback"]
    done = next(d for e, d in events if e == "done")["message"]["content"]
    assert "Iteration 3" in done and "Klick" in done
    after = client.portal.call(lambda: store.get_node(USER, task.id))
    assert after.body == FORM_BODY and after.next_iteration is None  # nothing before the click
