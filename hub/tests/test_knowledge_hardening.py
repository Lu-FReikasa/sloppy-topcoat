# ruff: noqa: E501, F811  (note texts are long; fixtures are imported by name)
"""The knowledge search, hardened after a measurement (four findings): a hit is the whole chunk,
a directory reference without lines is named by the note's first heading and listed once, the
directory file is never a chunk, and the secret filter needs a *value* behind the word "Passwort"
and says what it held back."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.test_knowledge import Docs, add_source, indexed, ready, search, world  # noqa: F401
from tests.test_knowledge_directory import DIRECTORY, with_directory
from topcoat_hub import knowledge_directory as kdir
from topcoat_hub import knowledge_rules as rules
from topcoat_hub import memory_filter

# --- 1. a hit is the whole chunk -------------------------------------------------------------

LONG = (
    "# Recovery\n\n## Disk\n\n"
    + " ".join(f"Step {i}: check the pool state and the cabling carefully." for i in range(1, 20))
    + " The recovery key of the NAS is stored in drawer seven of the grey cabinet."
)


def test_a_fact_at_the_end_of_a_chunk_is_in_the_hit(
    client: TestClient, world: dict[str, Any]
) -> None:
    assert 900 < len(LONG.split("\n\n", 2)[2]) <= rules.CHUNK_CHARS  # beyond the old excerpt
    world["docs"].files["recovery.md"] = LONG
    indexed(client, world)
    hits = search(client, "where is the recovery key of the NAS")
    top = next(h for h in hits if h["path"] == "recovery.md")
    assert "drawer seven of the grey cabinet" in top["text"] and not top["text"].endswith("…")
    assert len(top["text"]) <= rules.CHUNK_CHARS


def test_the_hit_size_is_a_setting_that_can_only_shorten(
    client: TestClient, world: dict[str, Any]
) -> None:
    world["docs"].files["recovery.md"] = LONG
    indexed(client, world)
    client.app.state.settings.knowledge_hit_chars = 300
    top = next(h for h in search(client, "recovery key NAS drawer") if h["path"] == "recovery.md")
    assert len(top["text"]) <= 302 and top["text"].endswith("…")
    assert client.get("/api/knowledge/status", headers=AUTH).json()["hit_chars"] == 300
    client.app.state.settings.knowledge_hit_chars = 99999  # more than a chunk holds: no change
    top = next(h for h in search(client, "recovery key NAS drawer") if h["path"] == "recovery.md")
    assert "drawer seven" in top["text"] and len(top["text"]) <= rules.CHUNK_CHARS


def test_a_directory_section_is_cut_at_the_same_limit(
    client: TestClient, world: dict[str, Any]
) -> None:
    docs = with_directory(world)
    docs.files["servers/web.md"] = "# Web\n\n## Restart\n\n" + "line of the runbook. " * 120
    indexed(client, world)
    hit = next(h for h in search(client, "web-stack") if h["via"] == "directory")
    assert len(hit["text"]) <= rules.CHUNK_CHARS + 2


# --- 2. a directory reference without lines ------------------------------------------------------

NO_LINES = DIRECTORY + "\n## proxy-box\n- `servers/web.md`\n\n## web-twin\n- `servers/web.md`\n"


def test_a_reference_without_lines_is_named_by_the_first_heading_not_by_the_entity(
    client: TestClient, world: dict[str, Any]
) -> None:
    with_directory(world, NO_LINES)
    indexed(client, world)
    hits = search(client, "problem with proxy-box")
    hit = next(h for h in hits if h["via"] == "directory" and h["entity"] == "proxy-box")
    assert hit["heading"] == "Web" and hit["heading"] != "proxy-box"
    assert hit["ord"] == 0 and "lines" not in hit


def test_the_same_section_is_not_listed_twice_from_the_directory_and_the_search(
    client: TestClient, world: dict[str, Any]
) -> None:
    with_directory(world, NO_LINES)
    indexed(client, world)
    # the directory gives the start of the web note (no lines); the similarity search finds the
    # chunk that is the same text under its own heading path
    hits = search(client, "proxy-box web container restarts memory limit compose", limit=8)
    keys = [(h["path"], h["ord"]) for h in hits if h.get("ord") is not None]
    assert len(keys) == len(set(keys)), keys
    assert [h["path"] for h in hits].count("servers/web.md") >= 1
    # two entries that name the same section show it once
    both = search(client, "proxy-box and web-twin", limit=8)
    assert [(h["path"], h["ord"]) for h in both if h["via"] == "directory"].count(
        ("servers/web.md", 0)
    ) == 1


def test_a_section_with_lines_keeps_its_heading_and_gets_its_chunk_number(
    client: TestClient, world: dict[str, Any]
) -> None:
    with_directory(world)
    indexed(client, world)
    hit = search(client, "problem with the web-stack")[0]
    assert hit["via"] == "directory" and hit["lines"] == "3-5"
    assert hit["heading"] == "Restart" and hit["ord"] == 0


def test_the_first_heading_names_the_start_of_a_note() -> None:
    text = "intro line\n\n# Title\nbody\n## Sub\nmore\n"
    assert kdir.section(text, kdir.Ref("t.md"))[0] == "Title"
    assert kdir.section("no headings at all here", kdir.Ref("t.md"))[0] == ""
    assert kdir.section(text, kdir.Ref("t.md", label="Setup"))[0] == "Setup"  # the label stays


# --- 3. the directory file is no chunk --------------------------------------------------------------


def _directory_chunks() -> int:
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM knowledge_chunks WHERE path LIKE :p"),
            {"p": f"%{kdir.DIRECTORY_FILE}"},
        ).scalar()
    engine.dispose()
    return int(n or 0)


def test_the_directory_file_is_never_indexed_as_chunks(
    client: TestClient, world: dict[str, Any]
) -> None:
    with_directory(world)
    source = indexed(client, world)
    assert _directory_chunks() == 0
    # it is a file of the source (the lookup reads it) but contributes nothing to the search
    assert source["files"] == 5
    assert all(h["path"] != kdir.DIRECTORY_FILE for h in search(client, "KI-Verzeichnis web-stack"))
    assert all(h["path"] != kdir.DIRECTORY_FILE for h in search(client, "servers/web.md Restart"))


def test_chunks_of_the_directory_from_an_older_index_are_removed_and_never_shown(
    client: TestClient, world: dict[str, Any]
) -> None:
    docs = with_directory(world)
    source = indexed(client, world)
    # an index made before the directory was kept out: its chunks are in the database
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        fid, sid, uid = conn.execute(
            text("SELECT id, source_id, user_id FROM knowledge_files WHERE path = :p"),
            {"p": kdir.DIRECTORY_FILE},
        ).one()
        conn.execute(
            text(
                "INSERT INTO knowledge_chunks (source_id, file_id, user_id, path, heading, ord, text, mtime) "
                "VALUES (:s, :f, :u, :p, 'web-stack', 0, 'web-stack servers/web.md Restart aliases webstack', 1)"
            ),
            {"s": sid, "f": fid, "u": uid, "p": kdir.DIRECTORY_FILE},
        )
        conn.execute(text("UPDATE knowledge_files SET chunk_count = 1 WHERE id = :f"), {"f": fid})
    engine.dispose()
    assert _directory_chunks() == 1
    # never returned, even before the next index run
    assert all(h["path"] != kdir.DIRECTORY_FILE for h in search(client, "aliases webstack Restart"))
    # the next index run removes them although the file did not change
    docs.reads.clear()
    assert (
        client.post(f"/api/knowledge/sources/{source['id']}/reindex", headers=AUTH).status_code
        == 200
    )
    ready(client, source["id"])
    assert _directory_chunks() == 0


# --- 4. the secret filter: a value, not the word -----------------------------------------------------

ABOUT_PASSWORDS = [
    "Anmeldung ohne Passwort ist an der Konsole möglich, sonst per SSH-Schlüssel.",
    "Das Passwort ist abgelaufen und muss im Portal erneuert werden, siehe Abschnitt vier.",
    "Passwort in 09 wechseln: der Termin steht im Kalender des Teams für den Monat.",
    "Zugang per Passwort ist deaktiviert; die PIN ist dem Admin bekannt und wird nicht notiert.",
    "Passwort: siehe Passwortmanager, Eintrag des Routers in der Gruppe Netzwerk.",
    "Das Passwort für den NAS ist abgelaufen und muss im Portal erneuert werden.",
]
REAL_CREDENTIALS = [
    "The admin password is hunter2hunter2 for the router login page.",
    "Passwort: hunter2x für das Webinterface des Routers im Keller",
    "Das Passwort ist Sommer2024 und gilt für das ganze Team im Büro",
    "password=abc12345 steht in der Datei des Backup-Skripts auf dem NAS",
    "Passwort: sonnenschein (nur für den Gast-Zugang des Netzes im Büro)",
    'Das Kennwort ist "geheim" und wird nie geändert',
    "Passwort ist: sonnenschein für den Zugang zum Switch im Rack",
    "Die PIN ist 4711 für das Türschloss der Garage im Keller des Hauses",
    "Das Passwort lautet aB3!xQ9 und gilt für den Export der Backups",
    "Das Passwort für den NAS ist Sommer2024 und gilt für alle im Büro",
    "Das Routerpasswort lautet aB3!xQ9 und steht nirgends sonst",
]


@pytest.mark.parametrize("sentence", ABOUT_PASSWORDS)
def test_text_that_only_mentions_the_word_passes_the_knowledge_filter(sentence: str) -> None:
    body, rule = rules.vet(sentence)
    assert body == sentence and rule is None


@pytest.mark.parametrize("sentence", REAL_CREDENTIALS)
def test_a_value_behind_the_word_is_still_held_back_in_the_knowledge_base(sentence: str) -> None:
    body, rule = rules.vet(sentence)
    assert body is None and rule == "password"


@pytest.mark.parametrize("sentence", [ABOUT_PASSWORDS[1], ABOUT_PASSWORDS[3], *REAL_CREDENTIALS])
def test_notes_and_everything_that_leaves_the_hub_keep_the_strict_rule(sentence: str) -> None:
    # the relaxation is for documentation only: the strict check (memory notes, answers, the
    # redaction before an external model) never gets weaker, whichever of the two a text is
    assert memory_filter.check(sentence) == "password"
    assert memory_filter.check("Anmeldung ohne Passwort an der Konsole möglich") is None


def test_names_of_switches_and_a_sentence_before_a_code_block_are_no_secrets() -> None:
    # found in the Outline wiki (E4-01): a field name with a yes/no value, a quoted field name in
    # a sentence, and "ohne Passwort … angelegt:" in front of a code block
    for note in (
        "4. **Falls doch und** `**reset_password=ja**`**:** Passwort neu setzen.",
        "Das Feld `reset_password` ist für halbfertige Konten gedacht: Wenn ein Benutzer schon "
        "existiert, lässt sich der Lauf wiederholen.",
        "| `reset_password` | `ja` setzt das Passwort neu; `nein` lässt es unangetastet |",
        "Einträge werden ohne Passwort über das Maschinenkonto angelegt:\n\n```bash\nsamba-tool x",
        "Ein Passwort für das Konto `Administrator` ist nirgends hinterlegt.",
    ):
        assert memory_filter.check(note, prose=True) is None, note
    # a value in a code block behind the word, or behind a quote, is still a secret
    for secret in (
        "Passwort:\n```\nhunter2xyz99\n```",
        "Das Passwort für den NAS:\n```\nhunter2xyz99\n```",
        "Das Passwort ist `Sommer2026!`",
        "admin_password=hunter2xyz99",
        "Anmeldung ohne Passwort: hunter2xyz99",
    ):
        assert memory_filter.check(secret, prose=True) == "password", secret


def test_the_other_rules_are_as_strict_in_the_knowledge_base_as_ever() -> None:
    for secret in (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc",
        "token sk-abcdefghijklmnop12345678",
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
        "https://admin:topsecret@host.example/path",
        "AKIAABCDEFGHIJKLMNOP",
    ):
        assert memory_filter.check(secret, prose=True) is not None, secret
        assert rules.vet(secret)[0] is None


def test_a_chunk_that_talks_about_passwords_is_indexed_and_a_chunk_with_one_is_not(
    client: TestClient, world: dict[str, Any]
) -> None:
    docs: Docs = world["docs"]
    docs.files["access.md"] = (
        "# Access\n\n## Login\n\nAnmeldung ohne Passwort ist an der Konsole möglich, sonst per SSH-Schlüssel des Admins.\n\n"
        "## Router\n\nThe admin password is hunter2hunter2 for the router login page of the basement.\n"
    )
    source = indexed(client, world)
    assert source["blocked"] == 1
    hits = search(client, "Anmeldung ohne Passwort Konsole")
    assert hits and hits[0]["path"] == "access.md" and "SSH-Schlüssel" in hits[0]["text"]
    for q in ("router login page basement", "admin password router", "hunter2hunter2"):
        assert all(
            "hunter2" not in h["text"] and h["heading"] != "Access > Router"
            for h in search(client, q)
        )


def test_what_the_filter_held_back_is_visible_with_path_rule_and_count(
    client: TestClient, world: dict[str, Any]
) -> None:
    docs: Docs = world["docs"]
    docs.files["ops.md"] = (
        "# Ops\n\n## Login\n\nThe admin password is hunter2hunter2 for the router login page.\n\n"
        "## Wifi\n\nPasswort: sonnenschein (nur für den Gast-Zugang des Netzes im Büro).\n\n"
        "## Fine\n\nAnmeldung ohne Passwort ist an der Konsole möglich, sonst per SSH-Schlüssel.\n"
    )
    source = indexed(client, world)
    assert source["blocked"] == 2
    detail = client.get(f"/api/knowledge/sources/{source['id']}", headers=AUTH).json()
    assert detail["blocked_files"] == [{"path": "ops.md", "rules": ["password"], "chunks": 2}]
    status = client.get("/api/knowledge/status", headers=AUTH).json()
    assert status["blocked"] == {
        "chunks": 2,
        "files": [{"source": "Notes", "path": "ops.md", "rules": ["password"], "chunks": 2}],
    }
    assert "hunter2" not in str(status)  # never the content


def test_a_section_held_back_via_the_directory_is_reported_in_the_search(
    client: TestClient, world: dict[str, Any]
) -> None:
    with_directory(world)
    indexed(client, world)
    r = client.get("/api/knowledge/search", headers=AUTH, params={"q": "what is on leaky"})
    body = r.json()
    assert not [h for h in body["hits"] if h["via"] == "directory"]
    assert body["directory"]["withheld"] == [{"path": "ops.md", "rule": "password"}]
    assert "hunter2" not in r.text


def test_the_status_lists_only_the_callers_own_sources(
    client: TestClient, world: dict[str, Any], clean_db: str
) -> None:
    from tests.users import make_user

    world["docs"].files["ops.md"] = (
        "# Ops\n\nThe admin password is hunter2hunter2 for the router.\n"
    )
    indexed(client, world)
    _, other = make_user(clean_db, "bea")
    assert client.get("/api/knowledge/status", headers=other).json()["blocked"] == {
        "chunks": 0,
        "files": [],
    }
    assert client.get("/api/knowledge/status", headers=AUTH).json()["blocked"]["chunks"] == 1
