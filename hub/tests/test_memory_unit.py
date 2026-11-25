"""Memory without I/O (E2-02): secret filter, hashing embedder, model-answer parsing, batches."""

import asyncio
import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from topcoat_hub import memory_clarify, memory_filter
from topcoat_hub.embedding import HashingEmbedder
from topcoat_hub.memory import (
    ChatActivity,
    _batches,
    entities,
    is_followup,
    is_personal,
    parse_json_object,
    statements,
    supports,
)
from topcoat_hub.memory_clarify import (
    Evidence,
    clash,
    conflict_groups,
    has_correction,
    has_retraction,
    judge,
    same_topic,
    slot_of,
    values_differ,
)
from topcoat_hub.memory_store import keyword_query, normalise_text

# --- secret filter ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("Das WLAN-Passwort ist Sommer2024!", "password"),
        ("password: hunter22", "password"),
        ("Die PIN lautet 4711", "password"),
        # a few words between the secret word and the verb, compounds, "set to" (E3 filter gap)
        ("Das Passwort für den NAS ist hunter2x", "password"),
        ("Das Passwort für den NAS ist Sommer2024!", "password"),
        ("Das Routerpasswort lautet abc123xyz", "password"),
        ("the password for the NAS is correcthorse", "password"),
        ("Der Admin-PIN des Routers lautet 4711", "password"),
        ("Das Kennwort wurde auf Blau7Fisch gesetzt", "password"),
        ("Das NAS-Kennwort wurde auf Blau7Fisch gesetzt", "password"),
        ("The password was set to Tr0ub4dor", "password"),
        ("Das Passwort ist jetzt Sommer2024", "password"),
        ("client_secret = abcdefgh", "password"),
        # the value right behind the word, no verb (prototype: adjacent values)
        ("Login admin mit Kennwort Sommer2026", "password"),
        ("NAS Passwort Tr0ub4dor", "password"),
        ("Use password hunter2x for the NAS", "password"),
        ("Der Schlüssel: sk-abcdefghijklmnop1234", "api_key"),
        ("Token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "api_key"),
        ("AWS AKIAIOSFODNN7EXAMPLE", "api_key"),
        ("hub key tc_abcdefghijklmnopqrstuvwxyz012345", "api_key"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "private_key"),
        ("Authorization: Bearer abcdefghijklmnop.qrstuvwx", "token"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk", "token"),
        ("mysql://admin:s3cret@db.example.lan/app", "url_credentials"),
        ("IBAN DE89 3704 0044 0532 0130 00", "iban"),
        ("Karte 4111 1111 1111 1111", "card_number"),
        ("Wert a3f9c2e1b7d84a56c0e9f1a2b3c4d5e6f7a8b9c0", "long_token"),
    ],
)
def test_secrets_are_refused_by_rule_name(text: str, rule: str) -> None:
    assert memory_filter.check(text) == rule


@pytest.mark.parametrize(
    "text",
    [
        "Wohnt in Hamburg und arbeitet als Tischler",
        "Nutzt einen Passwortmanager",
        "Prefers dark mode and short answers",
        "Hat zwei Kinder, Anna (7) und Ben (4)",
        "Telefon-Vorwahl 040, Postleitzahl 20095",
        "Die Bestellnummer 4111 1111 1111 1112 ist keine gültige Karte",  # fails Luhn
        "DE00 1234 5678 9012 3456 78 ist kein gültiger IBAN",  # fails mod 97
        "Projekt-ID 3f0c2a4e-8a51-4b7e-9a0e-1f2d3c4b5a60",  # a UUID is no secret
        "Der Server heißt node-nvidia und läuft unter Ubuntu",
        "Passwort-Manager ist installiert",
        "Die Anmeldung am NAS ist per Schlüssel möglich",
        "Er hat ein neues Passwort-System für das Büro vorgeschlagen",
        "Nutzt einen Passwort Manager",
        "Das Passwort zurücksetzen geht über das Webinterface",
        "Beim Passwort Wechsel meldet sich der NAS ab",
    ],
)
def test_ordinary_notes_pass(text: str) -> None:
    assert memory_filter.check(text) is None


# --- embedder -----------------------------------------------------------------------------------


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_hashing_embedder_is_deterministic_normalised_and_lexical() -> None:
    emb = HashingEmbedder()
    v1, v2 = asyncio.run(emb.embed(["Wohnt in Hamburg", "Wohnt in Hamburg"]))
    assert v1 == v2 and len(v1) == 384
    assert math.isclose(math.sqrt(sum(x * x for x in v1)), 1.0, rel_tol=1e-9)
    same = _cos(v1, emb.embed_one("wohnt in hamburg!"))
    related = _cos(v1, emb.embed_one("Wo wohne ich? In Hamburg?"))
    other = _cos(v1, emb.embed_one("Mag Espresso mit wenig Milch"))
    assert same > 0.99 > related > other
    assert related >= emb.recall_min > other  # the recall threshold separates the two
    # accents and case do not matter
    assert _cos(emb.embed_one("Käse"), emb.embed_one("KASE")) > 0.99
    assert emb.embed_one("...") == [0.0] * 384  # nothing to hash


def test_contradictions_are_not_near_duplicates_for_the_hashing_embedder() -> None:
    """Merging on write must never swallow a contradiction: only real duplicates merge."""
    emb = HashingEmbedder()
    a, b = emb.embed_one("Wohnt in Berlin"), emb.embed_one("Wohnt in Hamburg")
    assert _cos(a, b) < emb.dup_threshold
    assert _cos(a, emb.embed_one("wohnt in berlin.")) >= emb.dup_threshold


# --- model answers and helpers --------------------------------------------------------------------


def test_parse_json_object_finds_the_object_in_chatty_answers() -> None:
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('Sure!\n```json\n{"memories": [{"text": "x {y}"}]}\n```') == {
        "memories": [{"text": "x {y}"}]
    }
    assert parse_json_object('nope { not json } then {"ok": true}') == {"ok": True}
    assert parse_json_object("no json at all") is None
    assert parse_json_object('["a"]') is None
    assert parse_json_object('{"cut": "off') is None


def test_normalise_and_keyword_query() -> None:
    assert normalise_text("  a \n b\t c ") == "a b c"
    assert keyword_query("Wo wohne ich in Hamburg?") == "hamburg:* | ich:* | wohne:*"
    assert keyword_query("a b") is None


def test_chat_activity_needs_quiet_time() -> None:
    a = ChatActivity()
    assert a.idle_for(120)  # never chatted
    a.begin()
    assert not a.idle_for(0)
    a.end()
    assert not a.idle_for(120) and a.idle_for(0)


def test_batches_cluster_similar_notes_and_page_the_rest() -> None:
    ids = [uuid.UUID(int=i + 1) for i in range(30)]
    pairs = [(ids[0], ids[1], 0.9), (ids[1], ids[2], 0.8)]  # one cluster of three
    batches = _batches(set(ids[:5]), pairs)
    assert sorted(batches[0]) == sorted(ids[:3])
    rest = [i for b in batches[1:] for i in b]
    assert sorted(rest) == sorted(ids[3:5])
    # a cluster without anything unchecked is left alone; checked singletons never appear
    assert _batches({ids[10]}, pairs) == [[ids[10]]]
    # big clusters are cut, unchecked notes first
    chain = [(ids[i], ids[i + 1], 0.9) for i in range(20)]
    big = _batches({ids[20]}, chain)
    assert len(big[0]) == 12 and ids[20] in big[0]


def test_entities_are_the_values_a_note_states() -> None:
    assert entities("Lebt in Berlin") == {"berlin"}  # the leading verb is no value
    assert entities("Hat 2 Kinder, Anna und Ben") == {"2", "kinder", "anna", "ben"}
    assert entities("Der Nutzer wohnt in Kiel") == {"kiel"}
    # a leading word counts when the compared note has it too
    assert entities("Hund heißt Bello", ["Mein Hund heißt Bello"]) == {"hund", "bello"}
    assert entities("Hund heißt Bello") == {"bello"}
    # different homes never share their value; paraphrases do
    assert entities("Lebt in Berlin") != entities("Lebt und wohnt in Hamburg")
    assert entities("Wohnt in Hamburg") <= entities("Lebt in Hamburg an der Elbe")


def test_personal_messages_are_recognised() -> None:
    for text in (
        "Was weißt du über mich?",
        "kannst du mir sagen, wo ich wohne?",
        "Ich wohne jetzt in Berlin",
        "Wie heißt mein Hund?",
        "What do you know about me?",
        "Where do I live? My dog is called Bello",
        "Kennst du mich eigentlich?",
        "Wer bin ich?",
    ):
        assert is_personal(text), text
    for text in (
        "Erkläre mir bitte Quantenverschränkung kurz.",
        "Gib mir ein Rezept für Pasta",
        "Explain photosynthesis to us briefly",
        "Wie wird das Wetter morgen?",
        # my / I about a thing, not about the person (maintainer's hardware test)
        "Warum startet mein Steam nicht?",
        "Wie richte ich mein WLAN ein?",
        "Welche Geräte sind in meinem WLAN?",
        "Mein Code hat einen Fehler",
        "Why does my build fail?",
    ):
        assert not is_personal(text), text


def test_supports_means_the_users_own_message_states_the_note() -> None:
    assert supports("Ich wohne in Hamburg, wie gesagt.", "Wohnt in Hamburg")
    assert supports("mein hund heißt bello", "Hund heißt Bello")  # case does not matter
    assert supports("Mit meinem Hundes-Ding ist alles gut, Hund", "Hat einen Hund")  # a stem
    assert not supports("Was weißt du über mich?", "Wohnt in Hamburg")
    assert not supports("Mein WLAN-Passwort ist schwach.", "Lebt in Hamburg")
    assert not supports("Ich wohne in Berlin", "Wohnt in Hamburg")  # a different value
    assert not supports("Ich wohne in Hamburg", "Wohnt in Hamburg an der Elbe")  # extra value
    # a note without values needs most of its words
    assert supports("ich mag kurze antworten", "Mag kurze Antworten")
    assert not supports("kurze frage bitte", "Mag kurze Antworten")
    assert not supports("", "Wohnt in Hamburg")


def test_questions_state_nothing() -> None:
    assert statements("Wo wohne ich eigentlich, in Hamburg oder in Berlin?") == ""
    assert statements("Ich lebe in Hamburg. Und wo arbeite ich?") == "Ich lebe in Hamburg."
    assert statements("kannst du mir sagen, wo ich wohne") == ""  # a question without "?"
    assert statements("Ich wohne in Kiel, kannst du mir helfen.") != ""
    assert not supports("Wo wohne ich eigentlich, in Hamburg oder in Berlin?", "Lebt in Hamburg")
    assert supports("Ich lebe in Hamburg. Und wo arbeite ich?", "Lebt in Hamburg")


# --- the self-maintaining memory: slots, cues, judgement -----------------------------------------


def test_slots_find_notes_about_the_same_thing_without_shared_words() -> None:
    assert slot_of("Lebt in Musterstadt").name == "residence"  # type: ignore[union-attr]
    assert slot_of("Wohnt in Hamburg").name == "residence"  # type: ignore[union-attr]
    assert same_topic("Lebt in Musterstadt", "Wohnt in Hamburg")
    assert slot_of("Arbeitet als Tischler").name == "work"  # type: ignore[union-attr]
    assert slot_of("Hund heißt Bello").name == "pet"  # type: ignore[union-attr]  # not "name"
    assert slot_of("Heißt Anna").name == "name"  # type: ignore[union-attr]
    assert slot_of("Mag kurze Antworten") is None


def test_only_single_valued_slots_clash_and_a_refinement_is_no_conflict() -> None:
    assert clash("Lebt in Musterstadt", "Lebt in Hamburg")
    assert clash("Wohnt in Berlin", "Lebt in Hamburg")
    assert not clash("Arbeitet als Tischler", "Arbeitet als Bäcker")  # both may be true
    assert not clash("Wohnt in Hamburg", "Lebt in Hamburg an der Elbe")  # refinement
    assert not clash("Wohnt in Hamburg", "Arbeitet in Berlin")  # other slot
    assert values_differ("Lebt in Berlin", "Lebt in Hamburg")
    assert not values_differ("Wohnt in Hamburg", "Lebt in Hamburg an der Elbe")


@dataclass
class N:
    text: str
    created_at: datetime
    id: uuid.UUID = field(default_factory=uuid.uuid4)


def test_conflict_groups_connect_notes_that_clash() -> None:
    t = datetime(2026, 9, 29, tzinfo=UTC)
    a = N("Lebt in Hamburg", t)
    b = N("Lebt in Berlin", t + timedelta(days=1))
    c = N("Lebt in Musterstadt", t + timedelta(days=2))
    other = N("Arbeitet als Tischler", t)
    groups = conflict_groups([c, other, a, b])
    assert [[n.text for n in g] for g in groups] == [
        ["Lebt in Hamburg", "Lebt in Berlin", "Lebt in Musterstadt"]  # oldest first
    ]
    assert conflict_groups([a, other]) == []


def test_correction_and_retraction_cues_read_statements_not_questions() -> None:
    assert has_correction("ich wohne tatsächlich in Musterstadt")
    assert has_correction("Ich wohne nicht mehr in Berlin.")
    assert has_correction("I moved to Kiel.")
    assert not has_correction("Ich wohne in Berlin, nur zur Info.")
    assert not has_correction("Wohne ich jetzt in Berlin?")  # a question states nothing
    assert has_retraction("die beiden anderen Städte waren zum testen")
    assert has_retraction("Das mit Hamburg war falsch.")
    assert has_retraction("Berlin war nur ein Test")
    assert not has_retraction("Ich wohne in Berlin.")
    found = memory_clarify.test_sentences("Ich wohne in Kiel. Hamburg war nur ein Test.")
    assert [x.strip() for x in found] == ["Hamburg war nur ein Test."]


def test_followups_ask_about_the_last_answer() -> None:
    for text in ("Wie sicher bist du dir?", "Stimmt das wirklich?", "Woher weißt du das?"):
        assert is_followup(text), text
    assert is_followup("Are you sure?") and is_followup("how sure are you")
    assert not is_followup("Wo wohne ich?") and not is_followup("Erkläre mir Quantenphysik")


def test_judge_decides_only_when_it_is_clear() -> None:
    t = datetime(2026, 9, 29, 10, tzinfo=UTC)
    old = N("Lebt in Hamburg", t)
    new = N("Lebt in Berlin", t + timedelta(hours=1))
    notes = [old, new]
    # a plain newer statement of a personal fact: not clear, the user is asked
    plain = [
        Evidence(old.id, "statement", old.created_at),
        Evidence(new.id, "statement", new.created_at),
    ]
    assert judge(notes, plain) is None
    # the newer statement corrected the earlier one and nothing was said since
    fix = [plain[0], Evidence(new.id, "correction", new.created_at, "tatsächlich")]
    verdict = judge(notes, fix)
    assert verdict is not None and verdict.winner is new and verdict.losers == [old]
    assert "corrected it" in verdict.reason and "Lebt in Hamburg" in verdict.reason
    # the older note said again after the newer statement: it wins
    again = [*plain, Evidence(old.id, "confirmation", t + timedelta(hours=2))]
    verdict = judge(notes, again)
    assert verdict is not None and verdict.winner is old and "said it again" in verdict.reason
    # both said again at the very same time: nobody has the last word
    both = [*again, Evidence(new.id, "confirmation", t + timedelta(hours=2))]
    assert judge(notes, both) is None
    # a check that names the winner decides, whatever was said; two contradicting checks do not
    check = [*plain, Evidence(old.id, "check", t, "looked up", decisive_for=old.id)]
    verdict = judge(notes, check)
    assert verdict is not None and verdict.winner is old and "looked up" in verdict.reason
    clash_ = [check[2], Evidence(new.id, "check", t, "", decisive_for=new.id)]
    assert judge(notes, clash_) is None
    assert judge(notes, []) is None
