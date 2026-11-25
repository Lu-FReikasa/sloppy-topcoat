"""The two looks at a finished chat: summary by the author, check by another model."""

import asyncio
import json
import re
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_chat import new_conv, use_model
from tests.test_chat_review import LONG
from tests.test_memory import chat_once
from topcoat_hub import chat_digest
from topcoat_hub.digest_store import DigestStore
from topcoat_hub.gateway import (
    _IN_BACKGROUND_WORK,
    GatewayError,
    background_work,
    interactive_work,
)

A_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


SUMMARY_TEXT = "Es ging um Plex auf node-nvidia, der Neustart ist noch offen."


def statements_for(req: dict, text: str = SUMMARY_TEXT) -> str:
    """A pass-1 answer that cites the first user message of the request word for word."""
    body = req["messages"][1]["content"]
    mid, said = re.search(r"^\[(\d+)\] user: (.+)$", body, re.MULTILINE).groups()
    quote = said.strip()[:40]
    return json.dumps(
        {"statements": [{"text": text, "message_id": int(mid), "quote": quote, "role": "user"}]}
    )


def models(*entries: tuple[str, list[tuple[str, bool]]]) -> list[dict[str, Any]]:
    return [
        {"id": m, "topcoat": {"nodes": [{"node": n, "loaded": ld} for n, ld in nodes]}}
        for m, nodes in entries
    ]


# --- unit ------------------------------------------------------------------------------------


def test_the_checker_is_another_model_on_another_node_when_one_serves_it() -> None:
    ms = models(("gemma", [("node-a", True), ("node-b", True)]), ("qwen", [("node-a", True)]))
    # another model on the author's node comes before the author's model on a second node: a
    # check needs another view, not another machine (D87, issue 2.19)
    assert chat_digest.pick_checker(ms, "gemma", "node-a", "") == ("qwen", "node-a")
    one = models(("gemma", [("node-a", True)]), ("qwen", [("node-a", True)]))
    assert chat_digest.pick_checker(one, "gemma", "node-a", "") == ("qwen", "node-a")
    both = models(("gemma", [("node-a", True)]), ("qwen", [("node-a", True), ("node-b", True)]))
    assert chat_digest.pick_checker(both, "gemma", "node-a", "") == ("qwen", "node-b")
    # nothing else is loaded anywhere: the author's own model checks, on its own node
    alone = models(("gemma", [("node-a", True)]))
    assert chat_digest.pick_checker(alone, "gemma", "node-a", "") == ("gemma", "node-a")
    # a model that is only on disk is never loaded for a check
    assert (
        chat_digest.pick_checker(models(("qwen", [("node-a", False)])), "gemma", "node-a", "")
        is None
    )


def test_a_check_answer_is_cleaned_before_anything_is_stored() -> None:
    raw = json.dumps(
        {
            "summary": "Plex läuft auf node-nvidia, offen ist der Neustart.",
            "learned": [
                {"text": "alice nutzt Plex im Wohnzimmer", "kind": "person", "confidence": 0.95},
                {"text": "x", "kind": "person"},
                {"text": "Der Port ist 32400", "kind": "bogus", "volatile": True},
                {"nope": 1},
            ],
        }
    )
    summary, learned = chat_digest.parse_check("Antwort: " + raw)
    assert summary and summary.startswith("Plex läuft")
    assert [n["text"] for n in learned] == ["alice nutzt Plex im Wohnzimmer", "Der Port ist 32400"]
    assert learned[0]["confidence"] == chat_digest.LEARNED_CONFIDENCE_CAP  # never above a cap
    assert learned[1]["kind"] == "operational" and learned[1]["volatile"] is True
    assert chat_digest.parse_check("kein JSON") == (None, [])
    secret = json.dumps({"summary": "Passwort: Sommer2024xyz ist gesetzt", "learned": []})
    assert chat_digest.parse_check(secret)[0] is None


# --- with a hub ------------------------------------------------------------------------------


@pytest.fixture
def world(client: TestClient, connect):
    author = online_node(client, connect, "node-a", loaded=["small"])
    checker = online_node(client, connect, "node-b", loaded=["other"])
    use_model(client, "small")
    calls: list[tuple[str, str]] = []  # (who, kind of call)

    def answer_for(who: str):
        def answer(req: dict) -> str | None:
            system = req["messages"][0]["content"]
            if system.startswith("Note what the conversation below established"):
                calls.append((who, "summary"))
                return statements_for(req)
            if system.startswith("You check the summary"):
                calls.append((who, "check"))
                return json.dumps(
                    {
                        "summary": "Plex läuft auf node-nvidia, offen: Neustart (geprüft).",
                        "learned": [
                            {
                                "text": "Plex läuft auf node-nvidia",
                                "kind": "operational",
                                "confidence": 0.9,
                            },
                            {"text": "Passwort: Sommer2024xyz", "kind": "person"},
                        ],
                    }
                )
            if system.startswith("You review an answer"):
                calls.append((who, "review"))
                return json.dumps({"findings": []})
            return None

        return answer

    author.stream_text_fn = lambda req: LONG
    checker.stream_text_fn = lambda req: LONG
    author.answer_fn = answer_for("node-a")
    checker.answer_fn = answer_for("node-b")
    client.app.state.memory.settings.memory_idle_seconds = 0
    # in production the summary starts at once; here the tests start it themselves
    # the queue only holds the chats here; each test starts the work itself (no node is asked
    # by a side effect of a chat answer)
    client.app.state.digester.kick = lambda: None
    return client.app.state.digester, calls


def two_questions(client: TestClient) -> str:
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    chat_once(client, "Und wie starte ich es neu?", conv)
    return conv


def digest_of(client: TestClient, conv: str) -> Any:
    async def go() -> Any:
        async with client.app.state.sessionmaker() as db:
            return await DigestStore(db, A_ID).get(uuid.UUID(conv))

    return client.portal.call(go)


def notes(client: TestClient) -> list[dict[str, Any]]:
    engine = create_engine(client.app.state.settings.database_url)
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT kind, text, evidence, confidence, source_type, source_ref "
                "FROM memory_items WHERE user_id = :u AND status = 'active'"
            ),
            {"u": str(A_ID)},
        ).mappings()
        out = [dict(r) for r in rows]
    engine.dispose()
    return out


def test_a_finished_chat_is_summarised_by_the_background_model_and_checked_on_another_node(
    client: TestClient, world
) -> None:
    digester, calls = world
    conv = two_questions(client)
    assert client.portal.call(lambda: digester.close(A_ID, uuid.UUID(conv))) == "checked"
    assert ("node-a", "summary") in calls and ("node-b", "check") in calls
    d = digest_of(client, conv)
    assert (d.passes, d.status, d.learned) == (2, "checked", 1)
    assert d.author_model == "small" and d.author_node == "node-a"
    assert d.checker_model == "other" and d.checker_node != "node-a"
    assert d.summary == SUMMARY_TEXT  # only verified statements are text: the check keeps it
    # there is no separate hand-over note: the digest is what a new chat reads (D100)
    assert not [n for n in notes(client) if n["kind"] == "episode"]
    # no model reads the answers of the chat on its own any more (review_scheduled is off): a model
    # looks at an answer when the person asks (chat_review.review_now)
    assert ("node-b", "review") not in calls and ("node-a", "review") not in calls


def test_what_was_learned_is_an_unverified_note_and_secrets_are_refused(
    client: TestClient, world
) -> None:
    digester, _ = world
    conv = two_questions(client)
    client.portal.call(lambda: digester.close(A_ID, uuid.UUID(conv)))
    learned = [n for n in notes(client) if n["source_type"] == "chat_digest"]
    assert [n["text"] for n in learned] == ["Plex läuft auf node-nvidia"]
    assert learned[0]["evidence"] == "assumption"
    assert learned[0]["confidence"] <= chat_digest.LEARNED_CONFIDENCE_CAP
    assert not any("Sommer2024" in n["text"] for n in notes(client))


def test_the_same_state_is_not_looked_at_a_third_time(client: TestClient, world) -> None:
    digester, calls = world
    conv = two_questions(client)
    client.portal.call(lambda: digester.close(A_ID, uuid.UUID(conv)))
    before = list(calls)
    assert client.portal.call(lambda: digester.close(A_ID, uuid.UUID(conv))) == "skipped"
    assert calls == before
    # the chat goes on: it is looked at again, and the note learned twice is not stored twice
    chat_once(client, "Danke, und der Port?", conv)
    assert client.portal.call(lambda: digester.close(A_ID, uuid.UUID(conv))) == "checked"
    assert len([n for n in notes(client) if n["source_type"] == "chat_digest"]) == 1


def test_a_short_chat_is_left_alone(client: TestClient, world) -> None:
    digester, calls = world
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    assert client.portal.call(lambda: digester.close(A_ID, uuid.UUID(conv))) == "skipped"
    assert not [c for c in calls if c[1] in ("summary", "check")]


def test_the_second_look_does_not_wait_for_a_quiet_hub(client: TestClient, world) -> None:
    digester, calls = world
    conv = two_questions(client)
    digester.memory.settings.memory_idle_seconds = 3600  # somebody just chatted
    assert client.portal.call(lambda: digester.close(A_ID, uuid.UUID(conv))) == "checked"
    assert ("node-b", "check") in calls


def test_a_chat_is_not_digested_twice_at_the_same_time(client: TestClient, world) -> None:
    digester, calls = world
    conv = two_questions(client)
    digester._active[uuid.UUID(conv)] = A_ID  # a summary of this chat is running
    assert client.portal.call(lambda: digester.close(A_ID, uuid.UUID(conv))) == "busy"
    assert not calls


def test_without_a_loaded_model_the_digest_stays_at_pass_one(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client, "small")
    node.stream_text_fn = lambda req: LONG
    node.answer_fn = lambda req: (
        statements_for(req, "Es ging um Plex, der Neustart ist offen.")
        if req["messages"][0]["content"].startswith("Note what the conversation below")
        else None
    )
    digester = client.app.state.digester
    digester.memory.settings.memory_idle_seconds = 0
    digester.kick = lambda: None  # the test starts the work itself
    conv = two_questions(client)

    async def nothing_loaded() -> list[dict[str, Any]]:
        return models(("small", [("node-a", False)]))

    digester.gateway.list_models = nothing_loaded  # nothing is loaded: a check would need a load
    assert client.portal.call(lambda: digester.close(A_ID, uuid.UUID(conv))) == "no_checker"
    d = digest_of(client, conv)
    assert (d.passes, d.status) == (1, "no_checker")
    assert not [n for n in notes(client) if "Neustart ist offen" in n["text"]]


def test_a_chat_is_queued_once_with_every_answer_and_nothing_is_timed(
    client: TestClient, world
) -> None:
    digester, _ = world

    async def arm() -> int:
        a, b = uuid.uuid4(), uuid.uuid4()
        digester.after_answer(A_ID, a)
        digester.after_answer(A_ID, a)  # already waiting: it keeps its place
        digester.after_answer(A_ID, b)
        assert list(digester._pending) == [a, b]
        assert digester.work_state(a) == "waiting"
        n = len(digester._pending)
        digester.stop()
        return n

    assert client.portal.call(arm) >= 2
    # a real chat answer armed the timer of its conversation, and nothing runs on a clock
    conv = two_questions(client)
    assert uuid.UUID(conv) in digester._pending
    digester.stop()
    digester.settings.chat_digest_enabled = False
    chat_once(client, "noch was?", conv)
    assert uuid.UUID(conv) not in digester._pending


def test_after_a_restart_the_unfinished_chats_are_queued_once(client: TestClient, world) -> None:
    digester, _ = world
    done = two_questions(client)
    todo = two_questions(client)
    client.portal.call(lambda: digester.close(A_ID, uuid.UUID(done)))
    digester.stop()
    old = new_conv(client)
    chat_once(client, "alt", old)
    chat_once(client, "älter", old)
    engine = create_engine(client.app.state.settings.database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE messages SET created_at = now() - interval '5 days' "
                "WHERE conversation_id = :c"
            ),
            {"c": old},
        )
    engine.dispose()
    digester.stop()

    async def go() -> tuple[int, set[uuid.UUID]]:
        n = await digester.catch_up()
        keys = set(digester._pending)
        digester.stop()
        return n, keys

    n, keys = client.portal.call(go)
    # only the chat nothing has looked at: not the finished one, not the one of last week
    assert keys == {uuid.UUID(todo)} and n == 1


def test_a_summary_no_node_could_write_is_tried_again_not_given_up(
    client: TestClient, world
) -> None:
    digester, calls = world
    conv = two_questions(client)

    async def nobody(*_a: Any, **_k: Any) -> Any:
        raise GatewayError(503, "node_busy", "the node is busy (game or other programs)")

    real = digester.gateway.call
    digester.gateway.call = nobody
    assert client.portal.call(lambda: digester.close(A_ID, uuid.UUID(conv))) == "busy"
    assert digest_of(client, conv) is None
    digester.gateway.call = real  # the node is back: the same chat is done on the next try
    assert client.portal.call(lambda: digester.close(A_ID, uuid.UUID(conv))) == "checked"
    assert ("node-a", "summary") in calls


def test_the_summary_is_kept_current_after_every_answer(client: TestClient, world) -> None:
    digester, calls = world
    conv = two_questions(client)
    assert client.portal.call(lambda: digester.summarise_now(A_ID, uuid.UUID(conv))) == "ok"
    d = digest_of(client, conv)
    assert (d.status, d.author_node) == ("summarised", "node-a")
    assert not [c for c in calls if c[1] == "check"]  # the second look waits for the rest
    assert client.portal.call(lambda: digester.summarise_now(A_ID, uuid.UUID(conv))) == "skipped"
    chat_once(client, "Und der Port?", conv)
    assert client.portal.call(lambda: digester.summarise_now(A_ID, uuid.UUID(conv))) == "ok"
    assert digest_of(client, conv).last_message_id > d.last_message_id


def test_a_failing_node_is_asked_again_later_and_nobody_else(client: TestClient, world) -> None:
    digester, _ = world
    conv = two_questions(client)
    asked: list[str] = []

    async def refuse(target: str, *_a: Any, **_k: Any) -> Any:
        asked.append(target)
        raise GatewayError(504, "timeout", "the node did not answer in time")

    digester.gateway.call = refuse
    assert client.portal.call(lambda: digester.summarise_now(A_ID, uuid.UUID(conv))) == "busy"
    assert asked and all(a.endswith("@node-a") for a in asked)


def test_a_catch_up_for_one_user_reaches_back_as_far_as_asked(client: TestClient, world) -> None:
    digester, _ = world
    old = two_questions(client)
    engine = create_engine(client.app.state.settings.database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE messages SET created_at = now() - interval '30 days' "
                "WHERE conversation_id = :c"
            ),
            {"c": old},
        )
    engine.dispose()
    digester.stop()

    async def go() -> tuple[int, int, set[uuid.UUID]]:
        short = await digester.catch_up(A_ID)  # the default look-back: two days
        digester.stop()
        long = await digester.catch_up(A_ID, hours=24 * 365)
        keys = set(digester._pending)
        digester.stop()
        return short, long, keys

    short, long, keys = client.portal.call(go)
    assert short == 0 and long == 1 and keys == {uuid.UUID(old)}


def test_a_digest_of_another_user_is_not_visible(client: TestClient, world) -> None:
    from tests.users import make_user

    digester, _ = world
    conv = two_questions(client)
    client.portal.call(lambda: digester.close(A_ID, uuid.UUID(conv)))
    other_id, _ = make_user(client.app.state.settings.database_url, "mallory")

    async def go() -> Any:
        async with client.app.state.sessionmaker() as db:
            return await DigestStore(db, uuid.UUID(str(other_id))).get(uuid.UUID(conv))

    assert client.portal.call(go) is None


# --- what the nodes are doing (central view) -------------------------------------------------


def test_the_gateway_names_what_a_node_does_and_forgets_it_when_done(
    client: TestClient, world
) -> None:
    from topcoat_hub.gateway import work

    digester, _ = world
    gateway = digester.gateway
    with work("digest_summary"):
        key = gateway._track("node-a", "small")
    other = gateway._track("node-b", "other", "load")
    seen = {(b["node"], b["kind"]) for b in gateway.activity()}
    assert seen == {("node-a", "digest_summary"), ("node-b", "load")}
    gateway._untrack(key)
    gateway._untrack(other)
    assert gateway.activity() == []


def test_the_activity_view_lists_nodes_roles_and_the_digest(client: TestClient, world) -> None:
    from tests.conftest import AUTH
    from topcoat_hub.node_policy import AppRow, NodePolicy

    client.app.state.gateway.set_policy(
        NodePolicy(
            applications={
                "chat": AppRow(models=("other",), nodes=("node-b",)),
                "check": AppRow(models=("small",), nodes=("node-a",)),
            }
        )
    )
    conv = two_questions(client)  # an answer arms the digest timer
    body = client.get("/api/activity", headers=AUTH).json()
    nodes = {n["name"]: n for n in body["nodes"]}
    assert {"node-a", "node-b"} <= set(nodes) and nodes["node-a"]["work"] == []
    assert nodes["node-a"]["queued"] == {"chat": 0, "answer": 0, "idle": 0}
    roles = {r["key"]: r for r in body["roles"]}
    assert roles["digest"] == {"key": "digest", "model": "other", "node": "node-b"}
    assert roles["checker"]["model"] == "small" and roles["chat"]["model"]
    assert body["digest"]["waiting"] >= 1 and body["digest"]["running"] == 0
    assert client.get("/api/memory", headers=AUTH).json()["digest"]["waiting"] >= 1
    assert uuid.UUID(conv)


def test_busy_reason_says_why_background_work_waits(client: TestClient, world) -> None:
    digester, _ = world
    chat_once(client, "Wo läuft Plex?")  # somebody chatted just now
    digester.memory.settings.memory_idle_seconds = 0
    assert client.portal.call(digester.memory.busy_reason) is None
    digester.memory.settings.memory_idle_seconds = 3600
    assert "chatted" in (client.portal.call(digester.memory.busy_reason) or "")


def test_digest_now_arms_the_unfinished_chats_and_refuses_when_off(
    client: TestClient, world
) -> None:
    digester, _ = world
    two_questions(client)
    # a short chat is not worth a digest
    chat_once(client, "Wo läuft Plex?")
    r = client.post("/api/memory/digest", headers=AUTH)
    assert r.status_code == 202, r.text
    assert r.json() == {"started": 1}
    digester.stop()
    digester.settings.chat_digest_enabled = False
    r = client.post("/api/memory/digest", headers=AUTH)
    assert r.status_code == 409 and "digest_disabled" in r.text


# --- E5-01: citations checked by code -----------------------------------------------------------


class _Msg:
    def __init__(self, id: int, role: str, content: str) -> None:
        self.id, self.role, self.content = id, role, content


CHAT = [
    _Msg(10, "user", "Wo läuft Plex gerade?"),
    _Msg(11, "assistant", "Plex läuft auf node-nvidia, Port 32400."),
    _Msg(12, "user", "Dann starte ich es\n  heute Abend  neu."),
]


def stmt(text: str, mid: int | None, quote: str, role: str = "user") -> dict[str, Any]:
    return {"text": text, "message_id": mid, "quote": quote, "role": role}


def test_a_quote_that_is_in_the_message_verifies_whatever_its_case_or_spacing() -> None:
    out, counts = chat_digest.verify_citations(
        [
            stmt("Plex wird heute neu gestartet", 12, "„DANN starte ich es heute abend neu“"),
            stmt("Plex läuft auf node-nvidia", 11, "Plex läuft auf node-nvidia", "assistant"),
        ],
        CHAT,
    )
    assert [s["verified"] for s in out] == [True, True]
    assert counts == {"statements": 2, "verified": 2, "unverified": 0, "dropped": 0}


def test_a_missing_role_is_taken_from_the_message_and_a_long_quote_counts_by_its_start() -> None:
    long_quote = "Plex läuft auf node-nvidia, Port 32400. " + "und noch viel mehr Text " * 10
    chat = [*CHAT, _Msg(13, "assistant", long_quote + "Ende")]
    out, _ = chat_digest.verify_citations(
        [
            stmt("Plex läuft dort", 11, "Plex läuft auf node-nvidia", None),
            stmt("lang", 13, long_quote, "assistant"),
        ],
        chat,
    )
    assert [s["verified"] for s in out] == [True, True]
    assert out[0]["role"] == "assistant" and len(out[1]["quote"]) == chat_digest.QUOTE_MAX_CHARS


def test_a_changed_quote_a_wrong_role_or_a_short_quote_does_not_verify() -> None:
    out, counts = chat_digest.verify_citations(
        [
            stmt("a statement", 11, "Plex läuft auf node-amd, Port 32400."),
            stmt("a statement", 11, "Plex läuft auf node-nvidia", "user"),
            stmt("a statement", 11, "Plex"),
        ],
        CHAT,
    )
    assert [s["note"] for s in out] == ["quote_not_found", "role_mismatch", "bad_quote_length"]
    assert counts["verified"] == 0 and counts["unverified"] == 3


def test_a_message_of_another_chat_is_refused_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        out, counts = chat_digest.verify_citations(
            [
                stmt("not ours", 999, "Plex läuft auf node-nvidia", "assistant"),
                stmt("none", None, "x"),
            ],
            CHAT,
        )
    assert [s["note"] for s in out] == ["message_not_in_chat", "message_not_in_chat"]
    assert counts["verified"] == 0
    assert "outside the chat" in caplog.text


def test_a_secret_in_the_quote_or_the_text_removes_the_statement() -> None:
    chat = [_Msg(1, "user", "Mein Passwort: Sommer2024xyz ist gesetzt, ok?")]
    out, counts = chat_digest.verify_citations(
        [stmt("Das Passwort ist gesetzt", 1, "Passwort: Sommer2024xyz ist gesetzt")], chat
    )
    assert out == [] and counts["dropped"] == 1
    out, counts = chat_digest.verify_citations([], chat)  # an empty list is fine
    assert out == [] and counts["statements"] == 0


def test_the_text_holds_verified_statements_only_and_stays_within_the_limit() -> None:
    refs = [
        {"text": "old " + "x" * 600, "verified": True},
        {"text": "invented claim", "verified": False},
        {"text": "y" * 150, "verified": True},
    ]
    text = chat_digest.compose_summary(refs)
    assert "invented" not in text and len(text) <= chat_digest.SUMMARY_MAX
    assert text.endswith("y" * 150) and "old" not in text  # the newest statements win


def test_statements_are_read_with_numbers_as_text_and_extra_entries_cut() -> None:
    raw = json.dumps(
        {
            "statements": [
                {"text": "ok", "message_id": "[12]", "quote": "q", "role": "user"},
                {"nope": 1},
                *[stmt(f"s{i}", 10, "Wo läuft Plex") for i in range(9)],
            ]
        }
    )
    parsed = chat_digest.parse_statements("Antwort: " + raw)
    assert parsed[0]["message_id"] == 12 and len(parsed) == chat_digest.STATEMENTS_MAX
    assert chat_digest.parse_statements("kein JSON") == []


def test_an_invented_statement_is_kept_in_the_archive_but_never_in_the_text(
    client: TestClient, world
) -> None:
    digester, _ = world
    conv = two_questions(client)

    async def answer(*_a: Any, **_k: Any) -> Any:
        rows, _, _ = await digester._load(A_ID, uuid.UUID(conv))
        first = next(r for r in rows if r.role == "user")
        good = stmt("Wo Plex läuft wurde gefragt", first.id, first.content.strip()[:30])
        bad = stmt("Plex wurde gelöscht", first.id, "wörtlich nicht vorhanden")
        return {"choices": [{"message": {"content": json.dumps({"statements": [good, bad]})}}]}

    digester.gateway.call = answer
    assert client.portal.call(lambda: digester.summarise_now(A_ID, uuid.UUID(conv))) == "ok"
    d = digest_of(client, conv)
    assert d.summary == "Wo Plex läuft wurde gefragt"
    assert [(r["verified"], r["note"]) for r in d.summary_refs] == [
        (True, None),
        (False, "quote_not_found"),
    ]
    assert d.summary_refs[0]["learned_note_id"] is None


def test_a_summary_is_background_work_and_obeys_the_nodes_kept_free_for_it(
    client: TestClient, world
) -> None:
    digester, calls = world
    conv = two_questions(client)

    @background_work
    async def summarise() -> str:  # as `_fire_summary` runs it
        return await digester.summarise_now(A_ID, uuid.UUID(conv))

    # the node that answered the chat is kept free of background work by the admin: the summary
    # is background work, so it is written elsewhere and the digest says where
    r = client.put(
        "/api/scheduling/applications",
        headers=AUTH,
        json={"rows": {"background": {"models": ["small"], "nodes": ["node-b"]}}},
    )
    assert r.status_code == 200, r.text
    assert client.portal.call(summarise) == "ok"
    assert ("node-a", "summary") not in calls and ("node-b", "summary") in calls
    d = digest_of(client, conv)
    assert (d.status, d.author_model, d.author_node) == ("summarised", "small", "node-b")

    gw = client.app.state.gateway

    @background_work
    async def background_route() -> str:
        return (await gw.route("small@node-a")).candidates[0].name

    @background_work
    async def background_route_with_interactive_step() -> tuple[str, bool]:
        with interactive_work():
            inside = _IN_BACKGROUND_WORK.get()
            await gw.route("small@node-a")
        return "ok", inside

    # the same policy still blocks background work (pass 2, care, ...) on that node
    with pytest.raises(GatewayError) as exc:
        client.portal.call(background_route)
    assert exc.value.code == "node_kept_free"
    # the interactive block is checked as for chat; the flag is back to background afterwards
    assert client.portal.call(background_route_with_interactive_step) == ("ok", False)

    async def flag_after() -> bool:
        with interactive_work():
            pass
        return _IN_BACKGROUND_WORK.get()

    assert client.portal.call(flag_after) is False

    @background_work
    async def flag_restored() -> bool:
        with interactive_work():
            pass
        return _IN_BACKGROUND_WORK.get()

    assert client.portal.call(flag_restored) is True


def test_a_queued_chat_starts_at_once_and_a_busy_one_waits_for_a_free_node(
    client: TestClient, world
) -> None:
    digester, _ = world
    started: list[uuid.UUID] = []
    busy_once: set[uuid.UUID] = set()
    first, second = uuid.uuid4(), uuid.uuid4()
    busy_once.add(second)

    async def fake_close(user_id: uuid.UUID, conversation_id: uuid.UUID) -> str:
        if conversation_id in busy_once:
            busy_once.discard(conversation_id)
            return "busy"  # no node could take it now
        started.append(conversation_id)
        return "checked"

    async def go() -> None:
        del digester.kick  # the real queue: the fixture only holds it for the other tests
        digester.close = fake_close  # type: ignore[method-assign]
        digester.enqueue(A_ID, first)  # no timer: it starts at once
        digester.enqueue(A_ID, second)  # busy: it waits in the queue
        await asyncio.sleep(0.05)
        assert started == [first] and list(digester._pending) == [second]
        assert digester.work_state(second) == "waiting"
        digester.kick()  # a node frees a place: the waiting chat starts now
        await asyncio.sleep(0.05)
        assert started == [first, second] and not digester._pending

    client.portal.call(go)


def test_a_message_number_in_a_list_or_with_words_is_still_the_number() -> None:
    from topcoat_hub.chat_digest import parse_statements

    answer = (
        '{"statements": [{"text": "Plex: läuft", "message_id": [2715], "quote": "Plex läuft"},'
        ' {"text": "Port: 32400", "message_id": "message 2716", "quote": "Port 32400"}]}'
    )
    ids = [st["message_id"] for st in parse_statements(answer)]
    assert ids == [2715, 2716]
