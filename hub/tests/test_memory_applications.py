"""The core and the program memories (ARCHITECTURE D74): a program recalls the core and its own
memory, never another program's; a sensitive branch of the tag tree hides notes of the core from
the programs it was not released for; a new note lands where it was learned; the person moves
notes, sets tags and sorts what was learned before programs existed."""

import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub.memory_scope import Scope
from topcoat_hub.memory_store import MemoryStore
from topcoat_hub.models import DEV_ADMIN_ID, MemoryItem, MemoryItemTag


def tag_id(client: TestClient, name: str, user_headers: dict[str, str] = AUTH) -> uuid.UUID:
    tags = client.get("/api/memory/tags", headers=user_headers).json()["tags"]
    return uuid.UUID(next(t["id"] for t in tags if t["name"] == name))


def seed(
    text_: str,
    kind: str = "operational",
    application: str | None = "chat",
    tags: list[uuid.UUID] | None = None,
    user: uuid.UUID = DEV_ADMIN_ID,
    status: str = "active",
) -> str:
    engine = create_engine(TEST_DB_URL)
    with Session(engine) as s:
        item = MemoryItem(
            user_id=user,
            text=text_,
            kind=kind,
            application=application,
            status=status,
            confidence=0.9,
            evidence="doc",
            source_type="chat",
            source_ref={},
            version=1,
        )
        s.add(item)
        s.flush()
        for t in tags or []:
            s.add(MemoryItemTag(item_id=item.id, tag_id=t, user_id=user))
        s.commit()
        item_id = str(item.id)
    engine.dispose()
    return item_id


def notes(client: TestClient, scope: Scope | None, query: str = "alpha") -> list[str]:
    sessionmaker = client.app.state.sessionmaker

    async def read() -> list[str]:
        async with sessionmaker() as db:
            hits = await MemoryStore(db, DEV_ADMIN_ID, scope=scope).keyword(query, 50)
            return sorted(i.text for i, _ in hits)

    return client.portal.call(read)


def world(client: TestClient) -> uuid.UUID:
    health = tag_id(client, "health")
    seed("alpha core person fact", kind="person", application=None)
    seed("alpha chat day", application="chat")
    seed("alpha dev repo", application="dev")
    seed("alpha admin proxmox", application="admin")
    seed("alpha core health fact", kind="person", application=None, tags=[health])
    seed("alpha dev secret thing", application="dev", tags=[health])
    return health


def test_a_program_reads_the_core_and_its_own_memory_only(client: TestClient) -> None:
    health = world(client)
    dev = notes(client, Scope("dev", frozenset({health})))
    assert dev == ["alpha core person fact", "alpha dev repo", "alpha dev secret thing"]
    chat = notes(client, Scope("chat", frozenset({health})))
    assert chat == ["alpha chat day", "alpha core person fact"]
    assert "alpha admin proxmox" not in dev + chat


def test_no_scope_means_no_limit(client: TestClient) -> None:
    world(client)
    assert len(notes(client, None)) == 6  # the person's own page and the care see everything


def test_a_sensitive_branch_hides_the_core_until_it_is_released(client: TestClient) -> None:
    health = world(client)
    hidden = notes(client, Scope("admin", frozenset({health})))
    assert "alpha core health fact" not in hidden
    assert "alpha core health fact" in notes(client, Scope("admin", frozenset()))
    # a note of the program's own is not hidden from it by a tag
    assert "alpha dev secret thing" in notes(client, Scope("dev", frozenset({health})))


def test_releasing_a_branch_for_one_program_shows_it_there_only(client: TestClient) -> None:
    health = world(client)
    r = client.patch(f"/api/memory/tags/{health}", headers=AUTH, json={"released": ["admin"]})
    assert r.status_code == 200, r.text
    memory = client.app.state.memory

    async def recall(app: str) -> list[str]:
        got = await memory.recall(DEV_ADMIN_ID, "alpha", 800, application=app)
        return sorted(r.text for r in got)

    assert "alpha core health fact" in client.portal.call(recall, "admin")
    assert "alpha core health fact" not in client.portal.call(recall, "chat")


def test_recall_in_a_program_leaves_out_the_memories_of_the_others(client: TestClient) -> None:
    world(client)
    memory = client.app.state.memory

    async def recall(app: str) -> list[str]:
        got = await memory.recall(DEV_ADMIN_ID, "alpha", 800, application=app)
        return sorted(r.text for r in got)

    dev = client.portal.call(recall, "dev")
    assert (
        "alpha dev repo" in dev and "alpha chat day" not in dev and "alpha admin proxmox" not in dev
    )
    assert "alpha core health fact" not in dev  # sensitive: hidden for every program by default
    admin = client.portal.call(recall, "admin")
    assert "alpha admin proxmox" in admin and "alpha dev repo" not in admin


def test_a_word_of_the_question_that_is_a_tag_recalls_the_notes_below_it(
    client: TestClient,
) -> None:
    infra = tag_id(client, "infrastructure")
    r = client.post(
        "/api/memory/tags", headers=AUTH, json={"name": "backup", "parent_id": str(infra)}
    )
    assert r.status_code == 201, r.text
    backup = tag_id(client, "backup")
    seed("Läuft jede Nacht um drei auf die NAS", application="admin", tags=[backup])
    memory = client.app.state.memory

    async def recall(app: str) -> list[str]:
        got = await memory.recall(DEV_ADMIN_ID, "Wann läuft das Backup?", 800, application=app)
        return [r.text for r in got]

    assert client.portal.call(recall, "admin") == ["Läuft jede Nacht um drei auf die NAS"]
    assert client.portal.call(recall, "chat") == []  # Administration's memory, not Chat's


def test_a_new_note_belongs_where_it_was_learned_and_carries_a_tag(client: TestClient) -> None:
    conv = client.post("/api/conversations", headers=AUTH, json={"application": "dev"}).json()["id"]
    sessionmaker = client.app.state.sessionmaker

    async def learn() -> dict[str, tuple[str | None, list[str]]]:
        out: dict[str, tuple[str | None, list[str]]] = {}
        async with sessionmaker() as db:
            store = MemoryStore(db, DEV_ADMIN_ID)
            common: dict[str, Any] = {
                "embedding": None,
                "embedder": None,
                "evidence": "doc",
                "confidence": 0.8,
                "source_type": "chat",
                "reason": "test",
                "actor": "chat",
            }
            ref = {"conversation_id": conv}
            made = []
            for name, kind, extra in (
                ("operational", "operational", {}),
                ("person", "person", {}),
                ("preference", "preference", {}),
                ("explicit", "operational", {"application": "admin"}),
                ("core by hand", "operational", {"application": ""}),
                ("tagged", "operational", {"tags": ["development/pytest", "nonsense/x"]}),
                # "person" by the model's word, but about a thing: Development's memory
                ("thing", "person", {}),
            ):
                # what names the person stays in the core; a thing learned in Development does not
                said = {"person": "Der Nutzer alpha person", "thing": "Das alpha thing"}.get(
                    name, f"alpha {name}"
                )
                made.append(
                    (
                        name,
                        await store.add(said, kind=kind, source_ref=ref, **common, **extra),
                    )
                )
            made.append(
                (
                    "no conversation",
                    await store.add("alpha lone", kind="operational", source_ref={}, **common),
                )
            )
            names = await store.tag_names([i for _, i in made])
            out = {name: (i.application, names[i.id]) for name, i in made}
            await db.commit()
        return out

    got = client.portal.call(learn)
    assert got == {
        "operational": ("dev", ["development"]),
        "person": (None, ["person"]),
        "preference": (None, ["preferences"]),
        "explicit": ("admin", ["infrastructure"]),
        # a fact about a place never goes to the core: the default program takes it
        "core by hand": ("chat", ["everyday"]),
        # a new tag below a known one is made; one below an unknown parent is dropped
        "tagged": ("dev", ["pytest"]),
        "thing": ("dev", ["development"]),
        "no conversation": ("chat", ["everyday"]),
    }
    tags = {t["name"]: t for t in client.get("/api/memory/tags", headers=AUTH).json()["tags"]}
    assert tags["pytest"]["created_by"] == "agent" and tags["pytest"]["reviewed"] is False
    assert tags["pytest"]["effective"]["responsible"] == "dev"


def test_a_programs_page_shows_its_own_memory_only(client: TestClient) -> None:
    seed("alpha dev fact", application="dev")
    seed("alpha dev day", application="dev")
    seed("alpha core fact", kind="person", application=None)
    seed("alpha chat fact", application="chat")
    dev = client.get("/api/memory?application=dev", headers=AUTH).json()
    assert sorted(i["text"] for i in dev["items"]) == ["alpha dev day", "alpha dev fact"]
    core = client.get("/api/memory?application=core", headers=AUTH).json()
    assert [i["text"] for i in core["items"]] == ["alpha core fact"]
    assert (
        client.get("/api/memory?application=shared", headers=AUTH).json()["items"] == core["items"]
    )  # the old name of the core still answers
    assert dev["applications"] == {"dev": 2, "core": 1, "chat": 1}


def test_the_person_moves_a_note_and_tags_it(client: TestClient) -> None:
    item = seed("alpha moving", kind="person", application="chat")
    r = client.patch(
        f"/api/memory/{item}/placement",
        headers=AUTH,
        json={"application": "dev", "tags": ["person/Work", "development/x y"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["application"] == "dev" and r.json()["tags"] == ["work", "x-y"]
    r = client.patch(f"/api/memory/{item}/placement", headers=AUTH, json={"application": None})
    assert r.json()["application"] is None and r.json()["tags"] == ["work", "x-y"]  # tags stay
    # a tag finds the notes below it
    below = client.get("/api/memory?application=core&tag=person", headers=AUTH).json()
    assert [i["text"] for i in below["items"]] == ["alpha moving"]
    assert client.get("/api/memory?tag=unknown", headers=AUTH).json()["items"] == []
    assert (
        client.patch(
            f"/api/memory/{item}/placement", headers=AUTH, json={"application": "Bad Key"}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/memory/{uuid.uuid4()}/placement", headers=AUTH, json={"application": "dev"}
        ).status_code
        == 404
    )


def test_a_fact_about_a_place_cannot_go_to_the_core(client: TestClient) -> None:
    item = seed("alpha proxmox host", application="admin")
    r = client.patch(f"/api/memory/{item}/placement", headers=AUTH, json={"application": None})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "program_kind"


def test_nobody_else_can_move_or_see_a_note(client: TestClient, clean_db: str) -> None:
    _, other = make_user(clean_db, "user-b")
    item = seed("alpha private note", application="chat")
    r = client.patch(f"/api/memory/{item}/placement", headers=other, json={"application": "admin"})
    assert r.status_code == 404
    assert client.get("/api/memory?application=chat", headers=other).json()["items"] == []
    assert (
        client.get("/api/memory?application=chat", headers=AUTH).json()["items"][0]["application"]
        == "chat"
    )


def test_old_notes_are_sorted_by_suggestion_and_only_when_the_person_agrees(
    client: TestClient,
) -> None:
    dev = seed("Die Tests im Repository laufen mit pytest", application="chat")
    admin = seed("Home Assistant läuft als VM auf dem Proxmox-Host", application="chat")
    seed("Der Hund heißt Rex", application="chat")
    r = client.get("/api/memory/sort", headers=AUTH).json()["suggestions"]
    assert {s["id"]: s["suggested"] for s in r} == {dev: "dev", admin: "admin"}
    assert (
        client.get("/api/memory?application=dev", headers=AUTH).json()["items"] == []
    )  # nothing moved yet
    done = client.post(
        "/api/memory/sort", headers=AUTH, json={"assignments": [{"id": dev, "application": "dev"}]}
    )
    assert done.json() == {"moved": 1}
    assert [
        i["id"] for i in client.get("/api/memory?application=dev", headers=AUTH).json()["items"]
    ] == [dev]
    again = client.get("/api/memory/sort", headers=AUTH).json()["suggestions"]
    assert [s["id"] for s in again] == [admin]  # the accepted one is not suggested again


def test_the_consolidation_never_merges_across_memories(client: TestClient) -> None:
    """Two programs may know the same thing differently: that is the cross-check's, never a
    merge or a dispute of the consolidation."""
    a = seed("alpha same words", application="chat")
    b = seed("alpha same words", application="admin")
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE memory_items SET embedding = :v, embedder = 'e' WHERE id IN (:a, :b)"),
            {"v": "[1,0,0]", "a": a, "b": b},
        )
    engine.dispose()
    sessionmaker = client.app.state.sessionmaker

    async def pairs() -> list[Any]:
        async with sessionmaker() as db:
            return await MemoryStore(db, DEV_ADMIN_ID).similar_pairs("e", 0.5)

    assert client.portal.call(pairs) == []


def test_what_dev_knows_goes_stale_sooner() -> None:
    from topcoat_hub import memory_care

    def note(kind: str, application: str | None) -> MemoryItem:
        return MemoryItem(
            kind=kind,
            application=application,
            expires_at=None,
            confirmed_count=0,
            verified_count=0,
            last_confirmed_at=None,
            last_verified_at=None,
            evidence="doc",
            confidence=0.9,
        )

    cfg = memory_care.DEFAULT
    assert memory_care.halflife_days(note("operational", "dev"), cfg) == cfg.dev_halflife_days
    # the same kinds elsewhere keep their own half-life; a person fact in Dev is not shortened
    assert memory_care.halflife_days(note("operational", "admin"), cfg) == 14.0
    assert memory_care.halflife_days(note("person", None), cfg) == 365.0
