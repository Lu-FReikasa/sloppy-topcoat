"""One continuous conversation: segments by rest, hand-over, recall, archive."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH
from tests.fakenode import eventually, online_node
from tests.test_chat import ask, new_conv, sent_messages, use_model
from tests.test_memory import chat_once
from topcoat_hub import segments
from topcoat_hub.digest_store import DigestStore
from topcoat_hub.node_policy import AppRow, NodePolicy

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
A_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
SUMMARY = "Es ging um Plex auf node-nvidia, der Neustart ist noch offen."


def msg(i: int, minutes: float, role: str = "user", kind: str = "say", text_: str = "x") -> Any:
    return SimpleNamespace(
        id=i, created_at=NOW + timedelta(minutes=minutes), kind=kind, role=role, content=text_
    )


# --- unit ------------------------------------------------------------------------------------


def test_an_answer_written_by_a_flow_is_not_pinned_to_the_node_hub() -> None:
    from topcoat_hub.gateway import pin_target

    assert pin_target("gemma", "node-amd") == "gemma@node-amd"
    assert pin_target("gemma", "hub") == "gemma"  # the flow's own stamp, no node to pin to
    assert pin_target("gemma@node-amd", "node-laptop") == "gemma@node-amd"
    assert pin_target("gemma", None) == "gemma"


def test_a_rest_of_the_idle_time_ends_a_segment_and_progress_lines_do_not_count() -> None:
    rows = [msg(1, 0), msg(2, 1, "assistant"), msg(3, 30), msg(4, 31, "assistant")]
    segs = segments.split(rows, 1200)
    assert [(s.first_id, s.last_id, s.count) for s in segs] == [(1, 2, 2), (3, 4, 2)]
    # 19 minutes is no rest, 20 is
    assert len(segments.split([msg(1, 0), msg(2, 19)], 1200)) == 1
    assert len(segments.split([msg(1, 0), msg(2, 20)], 1200)) == 2
    # a progress line in the gap neither bridges nor starts a segment
    bridged = [msg(1, 0), msg(2, 10, kind="progress"), msg(3, 21)]
    assert len(segments.split(bridged, 1200)) == 2
    assert segments.split([], 1200) == []


def test_a_message_without_a_time_stays_with_the_one_before() -> None:
    rows = [msg(1, 0), SimpleNamespace(id=2, created_at=None, kind="say", role="user", content="")]
    assert [s.count for s in segments.split(rows, 1200)] == [2]


def test_a_break_between_two_messages_ends_a_segment_like_a_rest() -> None:
    rows = [msg(1, 0), msg(2, 1, "assistant"), msg(3, 3), msg(4, 4, "assistant")]
    brk = [NOW + timedelta(minutes=2)]
    segs = segments.split(rows, 1200, brk)
    assert [(s.first_id, s.last_id) for s in segs] == [(1, 2), (3, 4)]
    # a break before the first or after the last message cuts nothing
    assert len(segments.split(rows, 1200, [NOW - timedelta(minutes=5)])) == 1
    assert len(segments.split(rows, 1200, [NOW + timedelta(minutes=9)])) == 1
    # two breaks without a message between them make one cut, not an empty segment
    both = [NOW + timedelta(minutes=2), NOW + timedelta(minutes=2, seconds=30)]
    assert len(segments.split(rows, 1200, both)) == 2


def test_stored_breaks_are_parsed_and_junk_is_dropped() -> None:
    got = segments.parse_breaks(["2026-10-07T12:02:00+00:00", "nonsense", 5, "2026-10-07T12:01:00"])
    assert [b.isoformat() for b in got] == [
        "2026-10-07T12:01:00+00:00",
        "2026-10-07T12:02:00+00:00",
    ]
    assert segments.parse_breaks(None) == []


def test_recall_needs_two_shared_words_and_prefers_the_closer_match() -> None:
    rows = [
        SimpleNamespace(summary="Plex läuft auf node-nvidia, Netzteil offen"),
        SimpleNamespace(summary="Backup der Datenbank nach Host-a geplant"),
        SimpleNamespace(summary="Plex Neustart Skript geschrieben, node-nvidia getestet"),
    ]
    got = segments.pick_recalls("Wie war das mit dem Plex Neustart auf node-nvidia?", rows, 2)
    assert got == [rows[2], rows[0]]
    assert segments.pick_recalls("Wie ist das Wetter?", rows, 2) == []
    assert segments.pick_recalls("Plex", rows, 2) == []  # one word is a coincidence


def test_the_block_is_capped_and_empty_without_content() -> None:
    assert segments.block(None, None, [], 600) == ""
    long = "a" * 5000
    text_ = segments.block(long, NOW, [(NOW, "second")], 100)
    assert len(text_) <= 100 * segments.CHARS_PER_TOKEN + 2
    assert "second" not in text_  # the hand-over comes first and eats the room
    both = segments.block("zuletzt Plex", NOW, [(NOW, "früher Backup")], 600)
    assert "zuletzt Plex" in both and "früher Backup" in both
    assert both.index("zuletzt") < both.index("früher")


# --- with a hub ------------------------------------------------------------------------------


@pytest.fixture
def world(client: TestClient, connect):
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client, "small")
    node.stream_text_fn = lambda req: "Plex läuft auf node-nvidia."

    def answer(req: dict) -> str | None:
        if req["messages"][0]["content"].startswith("Summarise the conversation"):
            return SUMMARY
        return None

    node.answer_fn = answer
    client.app.state.settings.main_thread_enabled = True
    client.app.state.memory.settings.memory_idle_seconds = 0
    return node


def put_digest(client: TestClient, conv: str, summary: str = SUMMARY) -> None:
    """The running summary of a chat, as the digester leaves it after an answer."""
    last = max(
        m["id"] for m in client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    )

    async def go() -> None:
        async with client.app.state.sessionmaker() as db:
            await DigestStore(db, A_ID).put(
                uuid.UUID(conv), last, summary, ("small", "node-a"), datetime.now(UTC)
            )
            await db.commit()

    client.portal.call(go)


def age_messages(client: TestClient, conv: str, hours: float) -> None:
    engine = create_engine(client.app.state.settings.database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE messages SET created_at = created_at - make_interval(secs => :s) "
                "WHERE conversation_id = :c"
            ),
            {"s": hours * 3600, "c": conv},
        )
    engine.dispose()


def test_after_a_rest_the_prompt_starts_from_the_handover_not_from_the_old_messages(
    client: TestClient, world
) -> None:
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    chat_once(client, "Und wie starte ich es neu?", conv)
    put_digest(client, conv)
    age_messages(client, conv, 2)
    chat_once(client, "Wo waren wir stehen geblieben?", conv)
    prompt = sent_messages(world)
    assert [m["role"] for m in prompt] == ["system", "user"]  # old turns are not replayed
    assert "Where we stopped last time" in prompt[1]["content"]
    # the running summary of the chat is the hand-over, not a verbatim end
    assert SUMMARY in prompt[1]["content"]
    assert "not in the summary yet" not in prompt[1]["content"]  # it covers everything
    assert prompt[1]["content"].endswith("Wo waren wir stehen geblieben?")
    assert _summaries(client, conv) == []  # nothing is summarised for the section on top


def test_a_chat_without_a_running_summary_still_answers_from_the_verbatim_end(
    client: TestClient, world
) -> None:
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    chat_once(client, "Und wie starte ich es neu?", conv)
    age_messages(client, conv, 2)
    status, _, _ = ask(client, conv, "Wo waren wir stehen geblieben?")
    assert status == 200  # nothing waits for a summary
    prompt = sent_messages(world)
    assert [m["role"] for m in prompt] == ["system", "user"]
    assert "Where we stopped last time" in prompt[1]["content"]
    assert "Und wie starte ich es neu?" in prompt[1]["content"]  # the end of the old section


def test_what_the_running_summary_does_not_cover_yet_is_handed_over_as_it_was_said(
    client: TestClient, world
) -> None:
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    put_digest(client, conv)  # the summary is behind: the second exchange is not in it
    chat_once(client, "Und wie starte ich es neu?", conv)
    age_messages(client, conv, 2)
    chat_once(client, "Wo waren wir stehen geblieben?", conv)
    hand = sent_messages(world)[1]["content"]
    assert SUMMARY in hand and "not in the summary yet" in hand
    assert "Und wie starte ich es neu?" in hand
    assert "Wo läuft Plex?" not in hand  # that one is in the summary


def test_the_work_state_of_a_chat_tells_what_the_background_still_does(
    client: TestClient, world
) -> None:
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    digester = client.app.state.digester
    cid = uuid.UUID(conv)

    def state() -> str:
        return client.get(f"/api/conversations/{conv}/background", headers=AUTH).json()["state"]

    digester._pending.pop(cid, None)  # a chat queued after the answer is not what is looked at
    assert state() == "idle"
    digester._active[cid] = A_ID
    assert state() == "running"
    digester._active.pop(cid)
    digester._pending[cid] = A_ID  # a summary queued for a node that can take it
    assert state() == "waiting"
    digester._pending.pop(cid)
    assert (
        client.get(f"/api/conversations/{uuid.uuid4()}/background", headers=AUTH).status_code == 404
    )


def _summaries(client: TestClient, conv: str) -> list[str]:
    engine = create_engine(client.app.state.settings.database_url)
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT summary FROM chat_segments WHERE conversation_id = :c"), {"c": conv}
        )
        out = [r[0] for r in rows]
    engine.dispose()
    return out


def test_without_a_rest_or_with_the_switch_off_nothing_changes(client: TestClient, world) -> None:
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    chat_once(client, "Und wie starte ich es neu?", conv)
    prompt = sent_messages(world)
    assert [m["role"] for m in prompt] == ["system", "user", "assistant", "user"]
    assert "Where we stopped" not in prompt[-1]["content"]
    age_messages(client, conv, 2)
    client.app.state.settings.main_thread_enabled = False
    chat_once(client, "Weiter?", conv)
    assert len(sent_messages(world)) == 6  # the whole history, as before
    assert _summaries(client, conv) == []


def test_the_archive_lists_the_segments_and_marks_the_current_one(
    client: TestClient, world
) -> None:
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    age_messages(client, conv, 2)
    chat_once(client, "Noch eine Frage zu Plex", conv)
    body = client.get(f"/api/conversations/{conv}/segments", headers=AUTH).json()
    assert body["enabled"] is True and body["idle_seconds"] == 1200
    assert [s["current"] for s in body["segments"]] == [False, True]
    assert [s["messages"] for s in body["segments"]] == [2, 2]
    assert (
        client.get(f"/api/conversations/{uuid.uuid4()}/segments", headers=AUTH).status_code == 404
    )


def test_new_section_cuts_the_next_prompt_and_asking_twice_changes_nothing(
    client: TestClient, world
) -> None:
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    assert client.post(f"/api/conversations/{conv}/segments/break", headers=AUTH).json()["created"]
    again = client.post(f"/api/conversations/{conv}/segments/break", headers=AUTH).json()
    assert again["created"] is False
    chat_once(client, "Etwas ganz anderes", conv)
    body = client.get(f"/api/conversations/{conv}/segments", headers=AUTH).json()
    assert [s["current"] for s in body["segments"]] == [False, True]
    assert [s["messages"] for s in body["segments"]] == [2, 2]
    # the model of the second question saw only its own section (plus the hand-over)
    prompt = sent_messages(world)
    assert [m["role"] for m in prompt] == ["system", "user"]  # the old turn is not replayed
    assert "Where we stopped last time" in prompt[1]["content"]
    assert (
        client.post(f"/api/conversations/{uuid.uuid4()}/segments/break", headers=AUTH).status_code
        == 404
    )


def test_new_section_on_an_empty_conversation_is_a_no_op(client: TestClient) -> None:
    conv = new_conv(client)
    assert client.post(f"/api/conversations/{conv}/segments/break", headers=AUTH).json() == {
        "created": False
    }


# --- sections as chats of their own ------------------------------------------------------------


def section(client: TestClient, conv: str, force: bool = False) -> dict:
    r = client.post(f"/api/conversations/{conv}/section", headers=AUTH, json={"force": force})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_section_after_a_rest_is_a_chat_of_its_own_that_starts_from_the_handover(
    client: TestClient, world
) -> None:
    conv = new_conv(client)
    client.patch(
        f"/api/conversations/{conv}", headers=AUTH, json={"tool_scope": {"groups": ["docker"]}}
    )
    assert section(client, conv, force=True)["created"] is False  # empty chat: nothing to continue
    chat_once(client, "Wo läuft Plex?", conv)
    same = section(client, conv)  # used a moment ago: the same chat
    assert same["created"] is False and same["conversation"]["id"] == conv
    put_digest(client, conv)
    age_messages(client, conv, 2)
    nxt = section(client, conv)
    assert nxt["created"] is True
    new = nxt["conversation"]
    assert new["id"] != conv and new["continues_id"] == conv
    assert new["tool_scope"]["groups"] == ["docker"]  # the choice of servers goes along
    chat_once(client, "Wo waren wir stehen geblieben?", new["id"])
    prompt = sent_messages(world)
    assert [m["role"] for m in prompt] == ["system", "user"]  # nothing of the old chat replayed
    assert "Where we stopped last time" in prompt[1]["content"]
    assert SUMMARY in prompt[1]["content"]  # the running summary of the chat just left
    assert _summaries(client, conv) == []
    # both are ordinary chats: the archive lists them
    ids = {c["id"] for c in client.get("/api/conversations", headers=AUTH).json()}
    assert {conv, new["id"]} <= ids


def test_a_new_section_never_waits_for_a_summary(client: TestClient, world) -> None:
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    body = section(client, conv, force=True)  # no summary exists, the new chat starts anyway
    assert body["created"] is True and "summary_pending" not in body
    assert body["conversation"]["continues_id"] == conv
    assert _summaries(client, conv) == []


def test_new_section_on_request_and_the_thread_lists_the_chain(client: TestClient, world) -> None:
    first = new_conv(client)
    chat_once(client, "Wo läuft Plex?", first)
    put_digest(client, first)
    second = section(client, first, force=True)["conversation"]["id"]
    chat_once(client, "Etwas ganz anderes", second)
    third = section(client, second, force=True)["conversation"]["id"]
    body = client.get(f"/api/conversations/{third}/thread", headers=AUTH).json()
    assert [s["id"] for s in body["sections"]] == [first, second, third]
    assert [s["current"] for s in body["sections"]] == [False, False, True]
    assert [s["messages"] for s in body["sections"]] == [2, 2, 0]
    assert body["more"] is False
    assert _summaries(client, first) == []  # no section summary is written any more
    body = client.get(f"/api/conversations/{second}/thread", headers=AUTH).json()
    assert body["sections"][0]["summary"] == SUMMARY
    assert body["sections"][1]["summary"] is None  # the open one has none
    # the third chat's prompt: hand-over from the second, the first only as a related note
    chat_once(client, "Plex", third)
    prompt = sent_messages(world)
    assert [m["role"] for m in prompt] == ["system", "user"]
    assert "Where we stopped last time" in prompt[1]["content"]


def test_sections_stay_off_with_the_switch_and_for_unknown_chats(client: TestClient, world) -> None:
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    client.app.state.settings.main_thread_enabled = False
    off = section(client, conv, force=True)
    assert off["created"] is False and off["conversation"]["id"] == conv
    assert (
        client.post(f"/api/conversations/{uuid.uuid4()}/section", headers=AUTH, json={}).status_code
        == 404
    )
    assert client.get(f"/api/conversations/{uuid.uuid4()}/thread", headers=AUTH).status_code == 404


# --- node chain ------------------------------------------------------------------------------


def test_a_busy_first_node_hands_over_to_the_next_node_of_the_row(
    client: TestClient, connect
) -> None:
    amd = online_node(client, connect, "node-amd", loaded=["small"])
    online_node(client, connect, "node-laptop", loaded=["big"])
    use_model(client, "small")
    client.app.state.gateway.set_policy(
        NodePolicy(
            applications={"chat": AppRow(models=("small",), nodes=("node-amd", "node-laptop"))}
        )
    )
    conv = new_conv(client)
    _, events, _ = ask(client, conv, "eins")
    meta = events[0][1]
    assert (meta["node"], meta["model"], meta["fallback_from"]) == ("node-amd", "small", None)
    amd.state = "busy_gaming"
    amd.heartbeat()
    eventually(lambda: _node_state(client, "node-amd", "busy_gaming"))
    _, events, _ = ask(client, conv, "zwei")
    meta = events[0][1]
    assert (meta["node"], meta["model"]) == ("node-laptop", "small")
    assert (meta["fallback_from"], meta["fallback_state"]) == ("node-amd", "busy_gaming")
    last = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
    assert (last["node"], last["model"], last["fallback_from"]) == (
        "node-laptop",
        "small",
        "node-amd",
    )
    # a conversation with its own pick keeps it, the chain does not override it
    own = new_conv(client, model="big", node="node-laptop")
    _, events, _ = ask(client, own, "drei")
    assert events[0][1]["fallback_from"] is None


def _node_state(client: TestClient, name: str, state: str) -> None:
    nodes = {n["name"]: n["state"] for n in client.get("/api/nodes", headers=AUTH).json()}
    assert nodes[name] == state


def test_a_section_keeps_the_choice_of_servers_but_not_a_model_or_node_that_was_picked(
    client: TestClient, world
) -> None:
    """A pick belongs to the chat it was made in. The next section is decided by the table of
    applications, so a pick can never travel from section to section (and end in an error when its
    machine is gone)."""
    conv = new_conv(client, model="small", node="node-a")
    chat_once(client, "Wo läuft Plex?", conv)
    new = section(client, conv, force=True)["conversation"]
    assert new["created_at"] and new["continues_id"] == conv
    assert new["model"] is None and new["node"] is None
    assert (
        new["quality"] == client.get(f"/api/conversations/{conv}", headers=AUTH).json()["quality"]
    )
