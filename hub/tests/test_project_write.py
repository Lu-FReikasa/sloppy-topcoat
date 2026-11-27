"""Writing tasks without the agent loop (project_write.py, 2026-11-14).

Live: the loop wrote good task texts three times and failed on the JSON each time; the set that got
through held only titles. Now the hub reads, the chat model writes plain sections, the hub
builds the set."""

import uuid
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_chat import new_conv, use_model
from tests.test_chat_tools import ask_with
from topcoat_hub import project_write as pw
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_changesets import ChangeSetStore
from topcoat_hub.project_store import ProjectStore


def node(number: str, title: str, body: str = "", kind: str = "task", parent: Any = None) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(), number=number, title=title, body=body, kind=kind, parent_id=parent,
        status="todo",
    )  # fmt: skip


def test_a_write_request_and_its_tasks_are_recognised() -> None:
    assert pw.wants_writing("Bitte arbeite die 3 offenen Aufgaben in Topcoat genauer aus")
    assert pw.wants_writing("Überarbeite das nochmal und formuliere klare Aufgaben")
    assert pw.wants_writing("Bitte arbeite 1.1 und 1.2 in Topcoat genauer aus")  # dots inside
    assert not pw.wants_writing("Kannst du die erste Aufgabe anstoßen?")
    assert not pw.wants_writing("Welche Aufgaben sind offen?")
    assert pw.numbers_in("1.5 soll lesen, 1.6 recherchieren, dann 1.7") == ["1.5", "1.6", "1.7"]
    assert pw.numbers_in("die Aufgaben 1.5 bis 1.7") == ["1.5", "1.6", "1.7"]
    assert pw.numbers_in("Version 2 von 3") == []
    projects = [SimpleNamespace(title="Topcoat"), SimpleNamespace(title="Topcoat-Doku")]
    assert pw.pick_project(projects, ["in Topcoat-Doku bitte"]).title == "Topcoat-Doku"
    assert pw.pick_project(projects, ["nichts", "die Aufgaben in Topcoat"]).title == "Topcoat"
    assert pw.pick_project(projects, ["nichts hier"]) is None


def test_the_sections_become_a_change_set() -> None:
    parent = uuid.uuid4()
    a = node(
        "1.5",
        "Recherche",
        "Zielanwendung: dev\nErgebnisform: Ergebnis | Beleg\n- [ ] alt",
        parent=parent,
    )
    b = node("1.6", "Design", "Zielanwendung: research\n- [ ] alt", parent=parent)
    written = (
        "### 1.5 Recherche: Theme-Format\n- [ ] Code lesen\n- [ ] Format notieren\n\n"
        "Schritt für Schritt.\n\n"
        "### 1.9 Gibt es nicht\nText\n\n"
        "### neu Abnahme\nZielanwendung: project\n- [ ] prüfen\n"
    )
    plan = pw.build(pw.parse_sections(written), [a, b], ["1.5", "1.6"])
    update, create = plan.operations
    assert update["node"] == str(a.id) and update["title"] == "Recherche: Theme-Format"
    # the lines a run needs stay when the model left them out
    assert update["body"].startswith(
        "Zielanwendung: dev\nErgebnisform: Ergebnis | Beleg\n- [ ] Code lesen"
    )
    assert create == {
        "op": "create_node", "kind": "task", "title": "Abnahme",
        "body": "Zielanwendung: project\n- [ ] prüfen", "parent": str(parent),
    }  # fmt: skip
    assert plan.touched == ["1.5", "new: Abnahme"]
    assert plan.notes == ["1.9: no such task, left out", "1.6: not written"]


def test_a_rewrite_in_projekte_goes_through_one_call_and_waits_as_a_set(
    client: TestClient, connect
) -> None:
    fake = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    store = ProjectStore(client.app.state.sessionmaker)

    async def setup() -> tuple[Any, Any, Any]:
        project = await store.create_project(DEV_ADMIN_ID, title="Topcoat")
        topic = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="Themes")
        body = "Zielanwendung: dev\nErgebnisform: Ergebnis | Beleg\n- [ ] alt"
        one = await store.create_node(
            DEV_ADMIN_ID, project.id, kind="task", title="Recherche", body=body, parent_id=topic.id
        )
        two = await store.create_node(
            DEV_ADMIN_ID, project.id, kind="task", title="Bauen", body=body, parent_id=topic.id
        )
        return project, one, two

    _, one, _ = client.portal.call(setup)
    asked: list[dict[str, Any]] = []

    def answer(req: dict) -> str | None:
        if req["messages"][0]["content"].startswith("You write tasks"):
            asked.append(req)
            return (
                "### 1.1 Recherche: Theme-Format\n- [ ] Code lesen\n\nLies web/src/look.ts.\n\n"
                "### 1.2 Bauen: XP-Theme\n- [ ] Theme anlegen\n\nBaue das Theme."
            )
        return None

    fake.answer_fn = answer
    from topcoat_hub.chat_engine.answer import _write_target

    question = "Bitte arbeite 1.1 und 1.2 in Topcoat genauer aus"
    probe = SimpleNamespace(last_user_text=question, task_history=[("user", question)])
    target = client.portal.call(
        lambda: _write_target(
            SimpleNamespace(app=client.app), SimpleNamespace(uuid=DEV_ADMIN_ID), probe
        )
    )
    assert target is not None and target[2] == ["1.1", "1.2"]
    conv = new_conv(client, application="project")
    status, events, _ = ask_with(client, conv, "Bitte arbeite 1.1 und 1.2 in Topcoat genauer aus")
    kinds = [e for e, _ in events]
    assert status == 200 and kinds[-1] == "done", kinds
    assert "tools_start" not in kinds  # no agent loop
    assert len(asked) == 1 and "Bitte arbeite 1.1 und 1.2" in asked[0]["messages"][1]["content"]
    report = next(d for e, d in events if e == "report")["message"]
    draft = uuid.UUID(report["report"]["changes_for"])
    [row] = client.portal.call(
        lambda: ChangeSetStore(client.app.state.sessionmaker).for_task(DEV_ADMIN_ID, draft)
    )
    assert row.status == "pending" and len(row.operations) == 2
    assert all("body" in (p["change"] or {}) for p in row.preview)
    done = next(d for e, d in events if e == "done")["message"]["content"]
    assert "1.1, 1.2" in done and "wartet auf deinen Klick" in done
    # nothing changed before the click
    assert client.portal.call(lambda: store.get_node(DEV_ADMIN_ID, one.id)).title == "Recherche"
    assert client.get(f"/api/project/changesets?task_id={draft}", headers=AUTH).status_code == 200


def test_asking_to_apply_a_waiting_set_points_to_its_button_and_starts_no_run(
    client: TestClient, connect
) -> None:
    """Live 2026-11-18: "wende die Änderungen gern an" started a new run, which replaced the
    waiting set with another one and claimed the tasks were created. Only the click applies."""
    from topcoat_hub import chat_tools

    assert chat_tools.asks_to_apply("wende die Änderungen gern an")
    assert chat_tools.asks_to_apply("Bitte übernehmen")
    assert chat_tools.asks_to_apply("apply it please")
    assert not chat_tools.asks_to_apply("übernimm das, aber nenne die Aufgabe Modellabfrage")
    assert not chat_tools.asks_to_apply("Erstelle dafür bitte eine neue Aufgabe")

    fake = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    store = ProjectStore(client.app.state.sessionmaker)

    async def setup() -> None:
        project = await store.create_project(DEV_ADMIN_ID, title="Topcoat")
        topic = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="Themes")
        body = "Zielanwendung: dev\nErgebnisform: Ergebnis | Beleg\n- [ ] alt"
        for title in ("Recherche", "Bauen"):
            await store.create_node(
                DEV_ADMIN_ID, project.id, kind="task", title=title, body=body, parent_id=topic.id
            )

    client.portal.call(setup)
    hints: list[str] = []

    def answer(req: dict) -> str | None:
        if req["messages"][0]["content"].startswith("You write tasks"):
            return "### 1.1 Recherche: Format\n- [ ] Code lesen\n\nLies den Code."
        text = str(req["messages"][-1]["content"])
        if "apply the prepared changes" in text:
            hints.append(text)
            return "Der Satz wartet; klick auf «Angehakte anwenden»."
        return None

    fake.answer_fn = answer
    conv = new_conv(client, application="project")
    _, first, _ = ask_with(client, conv, "Bitte arbeite 1.1 in Topcoat genauer aus")
    assert "report" in [e for e, _ in first]  # the set waits
    status, events, _ = ask_with(client, conv, "wende die Änderungen gern an")
    kinds = [e for e, _ in events]
    assert status == 200 and kinds[-1] == "done", kinds
    assert "tools_start" not in kinds and "report" not in kinds  # no new run
    assert hints and "Angehakte anwenden" in hints[0] and "1.1: body, title «" in hints[0]
