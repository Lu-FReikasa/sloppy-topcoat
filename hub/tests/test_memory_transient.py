"""What is true for a moment is no memory (consolidation: the notebook kept the state of the day).

The first cases are lines the production hub's notebook really held; the second group are notes
that must still be kept. Placement: a fact about a repository belongs to the Development program,
not to the core that every program reads out."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from topcoat_hub import memory_filter
from topcoat_hub.application_brief import brief
from topcoat_hub.memory_scope import suggest_application

STATES = [
    "Der aktuelle Branch ist `topcoat/e8-01-trace-ui` auf Commit `00e6fa8a0393`.",
    "Die Laufzeit des Tests beträgt bereits 93 Sekunden.",
    "Systemstatus von Topcoat: Instabil. Docker-Container (scan-smb) beendet.",
    "Die Aufgabe E1-02 (llama.cpp-Runtime auf echten Knoten) ist bereits implementiert.",
    "Die Aufgabe E8-01 (Trace-Tool Web-Seite) ist aktuell und in Arbeit.",
    "Der Assistent hat die Werkzeuge genutzt, um den Status des Tests zu prüfen.",
    "Die Tests für E8-01 laufen bereits länger als erwartet, ohne Fehlermeldungen.",
    "The service is currently running on node-amd and failed twice.",
]
KEEP = [
    "Lebt in Hamburg und hat zwei Kinder.",
    "Bevorzugt kurze Antworten mit dem Befehl zuerst.",
    "Die Dokumentation der Server liegt im Outline-Wiki unter Infrastruktur.",
    "Der NAS heißt Nasbox und steht im Keller.",
    "Der Nutzer erwartet präzise Erklärungen, die auf direkten Fakten basieren.",
    "Sicherungen laufen jede Nacht um 3 Uhr auf den zweiten Server.",
]


@pytest.mark.parametrize("text", STATES)
def test_a_state_of_the_moment_is_not_noted(text: str) -> None:
    assert memory_filter.transient(text) is not None, text


@pytest.mark.parametrize("text", KEEP)
def test_a_lasting_fact_is_kept(text: str) -> None:
    assert memory_filter.transient(text) is None, text


def test_a_fact_about_a_repository_is_a_place_not_the_person() -> None:
    assert suggest_application("Arbeitet im Repository topcoat-localdev an der Aufgabe.") == "dev"
    assert suggest_application("Lebt in Hamburg.") is None


class _Repo:
    def __init__(self, name: str, kind: str) -> None:
        self.name, self.kind = name, kind


class _Repos:
    async def list(self, user_id: object) -> list[_Repo]:
        return [_Repo("topcoat-localdev", "local"), _Repo("docs", "github")]


class _State:
    sessionmaker = None
    devrepos = _Repos()


async def test_the_development_brief_names_the_registered_repositories() -> None:
    text = await brief(_State(), __import__("uuid").uuid4(), "dev")
    assert "Current state of this application" in text
    assert "- topcoat-localdev (local)" in text and "- docs (github)" in text


async def test_an_application_without_a_brief_adds_nothing() -> None:
    assert await brief(_State(), __import__("uuid").uuid4(), "chat") == ""
    assert await brief(_State(), __import__("uuid").uuid4(), None) == ""


async def test_the_project_brief_names_the_projects_and_counts_their_tasks(
    client: TestClient,
) -> None:
    from topcoat_hub.models import DEV_ADMIN_ID
    from topcoat_hub.project_store import ProjectStore

    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(DEV_ADMIN_ID, title="Topcoat Weiterentwicklung")
    await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="A")
    await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="B")
    text = await brief(client.app.state, DEV_ADMIN_ID, "project")
    assert "Topcoat Weiterentwicklung (active): 2 entries; tasks: 2 todo" in text
    assert text.startswith("Current state of this application")


def test_a_new_fact_about_a_repository_lands_in_development(client: TestClient) -> None:
    """The store places a person-kind note that is about a repository in the Development program."""
    from topcoat_hub.memory_store import MemoryStore
    from topcoat_hub.models import DEV_ADMIN_ID

    async def add() -> tuple[str, str | None]:
        async with client.app.state.sessionmaker() as db:
            store = MemoryStore(db, DEV_ADMIN_ID)
            item = await store.add(
                "Arbeitet im Repository topcoat-localdev an einer Aufgabe.",
                embedding=None,
                embedder=None,
                evidence="doc",
                confidence=0.8,
                source_type="chat",
                source_ref={},
                reason="test",
                actor="memory",
                kind="person",
            )
            await db.commit()
            return item.kind, item.application

    assert client.portal.call(add) == ("operational", "dev")
    assert AUTH


async def test_the_theme_reference_comes_only_when_the_talk_is_about_themes() -> None:
    uid = __import__("uuid").uuid4()
    about_code = await brief(_State(), uid, "dev", "Was macht agent_loop.py? Welche Limits hat er?")
    about_theme = await brief(_State(), uid, "dev", "Entwirf mir ein dunkles Cyberpunk-Theme")
    assert '"key": "dusk"' not in about_code and "- docs (github)" in about_code
    assert '"key": "dusk"' in about_theme
    assert '"key": "dusk"' in await brief(_State(), uid, "dev")  # no talk given: as before
