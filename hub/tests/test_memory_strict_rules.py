"""Stricter memory rules (2026-11-21): what a project holds itself, snapshots and notes about
notes are no memory; the hub settles a contradiction itself instead of asking the person.

The first group are lines the production notebook really held (they were read out in every chat
for days); the second group must still be kept."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.test_memory import chat_once, memory_items, model, note, setup_node
from topcoat_hub import memory_filter
from topcoat_hub.memory_clarify import auto_verdict, unproven
from topcoat_hub.memory_scope import about_the_person

NOT_MEMORY = [
    "Aufgabe 1.2 im Projekt 'Topcoat' hat den Status 'todo' und lautet 'Theme schreiben'.",
    "Für Aufgabe 1.2 müssen drei Theme-Ideen mit je einem Beispiel-JSON erstellt werden.",
    "Die Aufgabe 3.1.1 soll Informationen über Modelle auf node-desk und node-tower enthalten.",
    "Die Teilaufgaben 1.5, 1.6 und 1.7 im Projekt 'Topcoat' haben keine Abhängigkeiten.",
    "Das Projekt 'Topcoat' enthält fünf Aufgaben, die alle im Status 'todo' sind.",
    "Eine neue Aufgabe im Projekt Topcoat wurde angelegt, um nach Modellen zu recherchieren.",
    "Die Aufgabe zur Baumansicht wurde vorbereitet und wartet auf Bestätigung durch den Operator.",
    "Der neueste Commit im Repository sloppy-topcoat hat eine Test-Datei hinzugefügt.",
    "Beide Projekte wurden in den letzten 24 Stunden erstellt.",
    "Die Notizen über die Agenten-Limits sind veraltet und widersprechen der zweiten Antwort.",
    "Der Server node-tower wird zurzeit genutzt, um das Modell gemma-4 zu betreiben.",
]
STILL_MEMORY = [
    "Lebt in Hamburg und hat zwei Kinder.",
    "Der Nutzer bevorzugt, neue Features in drei Aufgaben aufzuteilen: Recherche, Dev und Test.",
    "Themes in sloppy-topcoat-local werden über ein ThemeManifest-Objekt definiert.",
    "node-amd hat einen AMD Ryzen 7 3700X, 32 GB RAM und eine Radeon RX 9070 XT.",
    "Die Nextcloud soll nachts um 3 Uhr gesichert werden.",
    "Der Nutzer will immer die neueste Version eines Modells sehen.",
]


@pytest.mark.parametrize("text", NOT_MEMORY)
def test_what_a_project_holds_or_a_moment_shows_is_no_memory(text: str) -> None:
    assert memory_filter.transient(text) is not None, text


@pytest.mark.parametrize("text", STILL_MEMORY)
def test_a_lasting_fact_is_still_kept(text: str) -> None:
    assert memory_filter.transient(text) is None, text


def test_a_sentence_about_the_person_names_them() -> None:
    assert about_the_person("Der Nutzer möchte ein dunkles Theme.")
    assert about_the_person("Mein Hund heißt Bello.")
    assert about_the_person("Mag kurze Antworten")  # the neutral form: the person is the subject
    assert not about_the_person("Das Theme-Format wird über ein Manifest definiert.")
    assert not about_the_person("Die Taskleiste im Theme 'cyberpunk' ist oben positioniert.")


@dataclass
class _Note:
    text: str
    evidence: str
    created_at: datetime
    last_confirmed_at: datetime | None = None
    last_verified_at: datetime | None = None
    id: uuid.UUID = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.id = uuid.uuid4()


T0 = datetime(2026, 11, 1, tzinfo=UTC)


def test_the_stronger_evidence_wins_without_asking() -> None:
    told = _Note("Qwen3 hat 32k Kontext.", "doc", T0)
    guessed = _Note("Qwen3 hat 256k Kontext.", "assumption", T0 + timedelta(days=3))
    verdict = auto_verdict([told, guessed])
    assert verdict is not None and verdict.winner is told and verdict.losers == [guessed]
    assert "stronger evidence" in verdict.reason


def test_among_equals_the_latest_word_wins() -> None:
    old = _Note("Backups laufen um 3 Uhr.", "doc", T0)
    new = _Note("Backups laufen um 2 Uhr.", "doc", T0 + timedelta(days=1))
    assert auto_verdict([old, new]).winner is new  # type: ignore[union-attr]
    # a confirmation of the older note after that makes it the latest word
    old.last_confirmed_at = T0 + timedelta(days=2)
    assert auto_verdict([old, new]).winner is old  # type: ignore[union-attr]


def test_a_live_check_beats_a_statement() -> None:
    said = _Note("Der NAS hat 4 TB.", "doc", T0 + timedelta(days=5))
    seen = _Note("Der NAS hat 8 TB.", "live", T0, last_verified_at=T0)
    assert auto_verdict([said, seen]).winner is seen  # type: ignore[union-attr]


def test_a_plain_contradiction_in_chat_is_settled_without_a_question(
    client: TestClient, connect
) -> None:
    client.app.state.settings.memory_auto_settle = True
    node = setup_node(client, connect)
    node.answer_fn = model(
        {"Berlin": [note("Wohnt in Berlin")], "Hamburg": [note("Wohnt in Hamburg")]}
    )
    chat_once(client, "Ich wohne in Berlin, nur zur Info.")
    chat_once(client, "Ich wohne in Hamburg, nur zur Info.")
    assert [i["text"] for i in memory_items(client, status="active")] == ["Wohnt in Hamburg"]
    assert [i["text"] for i in memory_items(client, status="superseded")] == ["Wohnt in Berlin"]
    assert client.get("/api/memory/questions", headers=AUTH).json() == []


def test_two_guesses_are_not_decided_by_which_came_last() -> None:
    # live 2026-11-21: the newer guess was the wrong one ("themes contain executable code")
    right = _Note("Themes bestehen aus JSON und CSS.", "assumption", T0)
    wrong = _Note("Themes enthalten ausführbaren Code.", "assumption", T0 + timedelta(hours=1))
    assert auto_verdict([right, wrong]) is None
    assert unproven([right, wrong])
    assert not unproven([right, _Note("Themes sind Code.", "doc", T0)])


def test_contradicting_guesses_are_both_archived_without_a_question(client: TestClient) -> None:
    from topcoat_hub.memory_store import MemoryStore
    from topcoat_hub.models import DEV_ADMIN_ID

    client.app.state.settings.memory_auto_settle = True
    service = client.app.state.memory

    async def run() -> None:
        async with client.app.state.sessionmaker() as db:
            store = MemoryStore(db, DEV_ADMIN_ID)
            made = [
                await store.add(
                    text,
                    embedding=None,
                    embedder=None,
                    evidence="assumption",
                    confidence=0.5,
                    source_type="chat_digest",
                    source_ref={},
                    reason="test",
                    actor="digest",
                    kind="operational",
                )
                for text in ("Themes bestehen aus JSON und CSS.", "Themes enthalten Code.")
            ]
            await service._doubt(store, made, "consolidation")
            await db.commit()

    client.portal.call(run)
    archived = {i["text"] for i in memory_items(client, status="archived")}
    assert archived == {"Themes bestehen aus JSON und CSS.", "Themes enthalten Code."}
    assert client.get("/api/memory/questions", headers=AUTH).json() == []


def test_a_perfect_tie_is_not_decided() -> None:
    a = _Note("A ist 1.", "doc", T0)
    b = _Note("A ist 2.", "doc", T0)
    assert auto_verdict([a, b]) is None
    assert auto_verdict([a]) is None
