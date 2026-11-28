"""Message kinds (E3-07 D3): a request that needs a finding is a confirmation bubble, a progress
line, the report card the hub builds from the check, and a closing bubble that answers the question.
The model writes neither the confirmation nor the card and does not repeat it."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_chat import new_conv, sent_messages, use_model
from tests.test_chat_tools import ask_with, script_check
from tests.test_tools import tools_node
from topcoat_hub import chat, report
from topcoat_hub.chat_engine.common import CONFIRMATIONS, REPORT_LEAD, history_pairs, hub_language
from topcoat_hub.models import AgentEvidence, Message

LONG_CLOSING = (
    "Fertig, web ist am Speicherlimit gescheitert. Das Limit ist vermutlich zu niedrig. "
    "Dritte Aussage. Vierte Aussage. Fünfte Aussage. Sechste Aussage. "
    "Siebte, die nicht mehr in die Schlussblase gehört. Und eine achte."
)


@pytest.fixture
def world(client: TestClient, connect):
    """A tool node with a canned log and a model node that answers the chat."""
    node_id, tnode = tools_node(client, connect, "vm-docker")
    tnode.capability_answers["docker.logs"] = {
        "container": "web",
        "lines": 3,
        "log": "2026-09-30T07:59:00Z starting\n2026-09-30T07:59:05Z fatal: out of memory\n",
        "truncated": False,
        "masked": 0,
    }
    model = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    return node_id, tnode, model


def _task(**kw: Any) -> Any:
    base: dict[str, Any] = {
        "id": "11111111-1111-4111-8111-111111111111",
        "status": "done",
        "stop_reason": None,
        "tool_calls": 2,
        "diagnosis": {
            "summary": "web ran out of memory.",
            "findings": [
                {"claim": "web was killed for memory", "tag": "live", "evidence": ["E1"]},
                {
                    "claim": "the note about port 80 is outdated",
                    "tag": "live",
                    "evidence": ["E2"],
                    "contradicts": ["E3"],
                },
                {
                    "claim": "the limit may be too low",
                    "tag": "assumption",
                    "confirm_with": "docker.inspect",
                },
            ],
            "next": "raise the memory limit",
            "impact": "web restarts once",
            "rollback": "set the old limit again",
            "notes": [],
        },
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _ev(ref: str, text: str, capability: str = "docker.logs", tag: str = "live") -> AgentEvidence:
    return AgentEvidence(
        ref=ref,
        tag=tag,
        source={"capability": capability, "node": "vm-docker", "input": {"container": "web"}},
        excerpt=text,
        bytes=len(text),
    )


# --- the card ----------------------------------------------------------------------------------


def test_the_card_keeps_findings_and_assumptions_apart_and_links_every_mark() -> None:
    card = report.build(_task(), [_ev("E1", "oom"), _ev("E2", "ports")])
    assert card is not None
    assert card["summary"] == "web ran out of memory."
    assert [f["claim"] for f in card["findings"]] == [
        "web was killed for memory",
        "the note about port 80 is outdated",
    ]
    assert card["findings"][0]["evidence"] == ["E1"] and card["findings"][0]["tag"] == "live"
    assert card["findings"][1]["contradicts"] == ["E3"]
    assert [a["claim"] for a in card["assumptions"]] == ["the limit may be too low"]
    assert card["assumptions"][0]["confirm_with"] == "docker.inspect"
    assert [e["ref"] for e in card["evidence"]] == ["E1", "E2"]
    assert card["evidence"][0]["node"] == "vm-docker" and card["evidence"][0]["excerpt"] == "oom"
    assert card["incomplete"] is False and card["calls"] == 2


def test_a_proposal_carries_impact_and_rollback_or_says_they_are_missing() -> None:
    card = report.build(_task(), [_ev("E1", "x")])
    assert card is not None
    assert card["proposal"] == {
        "text": "raise the memory limit",
        "impact": "web restarts once",
        "rollback": "set the old limit again",
        "advice_only": True,
    }
    bare = _task(diagnosis={"summary": "s", "findings": [], "next": "do x", "notes": []})
    card = report.build(bare, [])
    assert card is not None and card["proposal"]["impact"] == ""
    text = report.text(card)
    # what the check did not state is left out, not filled in with a phrase
    assert "Impact" not in text and "Rollback" not in text
    assert (
        report.build(_task(diagnosis={"summary": "s", "findings": [], "notes": []}), []) is not None
    )
    assert report.build(None, []) is None  # nothing seen, nothing concluded: no card


def test_a_failed_check_with_evidence_is_an_incomplete_card() -> None:
    task = _task(status="failed", stop_reason="bad_format", diagnosis=None)
    card = report.build(task, [_ev("E1", "oom")], error="the model's answer was cut off")
    assert card is not None
    assert card["incomplete"] is True and "cut off" in card["incomplete_reason"]
    assert card["findings"] == [] and card["evidence"][0]["ref"] == "E1"
    assert report.text(card).startswith("**Incomplete check")


def test_an_incomplete_card_names_why_in_words() -> None:
    task = _task(status="done", stop_reason="time_budget")
    card = report.build(task, [_ev("E1", "oom")])
    assert card is not None and card["incomplete"] is True
    assert report.text(card).startswith("**Incomplete check: it ran out of time.**")
    # a check that stopped looking (repeats, no progress) with findings from evidence answered:
    # it is not called incomplete (live 2026-11-12)
    task = _task(status="done", stop_reason="repeated_calls")
    card = report.build(task, [_ev("E1", "oom")])
    assert card is not None and card["incomplete"] is False
    task = _task(status="done", stop_reason="something_new")
    card = report.build(task, [_ev("E1", "oom")])
    assert card is not None
    assert report.text(card).startswith("**Incomplete check: something_new.**")


def test_long_evidence_is_cut_the_log_keeps_its_end() -> None:
    log = "\n".join(f"line {i}" for i in range(2000))
    card = report.build(_task(), [_ev("E1", log), _ev("E2", log, capability="disk.usage")])
    assert card is not None
    first, second = card["evidence"]
    assert len(first["excerpt"]) <= report.EXCERPT_CHARS and first["truncated"]
    assert first["excerpt"].endswith("line 1999")  # newest lines of a log
    assert second["excerpt"].startswith("line 0")  # the start of anything else


def test_the_text_of_the_card_names_every_part() -> None:
    card = report.build(_task(), [_ev("E1", "oom"), _ev("E2", "ports")])
    assert card is not None
    text = report.text(card)
    for needle in (
        "**Summary:** web ran out of memory.",
        "- [live] web was killed for memory [E1]",
        "contradicts the notes E3",
        "**Assumptions (not seen, not confirmed):**",
        "(to check: docker.inspect)",
        "**Proposal (advice only, nothing was changed):** raise the memory limit",
        "Impact: web restarts once",
        "Rollback: set the old limit again",
        "**Evidence:** E1 (docker.logs on vm-docker)",
    ):
        assert needle in text


# --- the pieces of the conversation ------------------------------------------------------------


def test_the_hub_speaks_the_language_of_the_user() -> None:
    assert hub_language("", "Läuft Plex gerade?") == "de"  # the message decides when it is clear
    assert hub_language("de", "Is plex running?") == "en"  # even against the GUI language
    assert hub_language("en", "Läuft Plex?") == "de"
    assert hub_language("de", "Plex?") == "de"  # unclear: the setting
    assert hub_language("", "Plex?") == "en"
    assert set(CONFIRMATIONS) == {"de", "en"}


def _msg(role: str, content: str, kind: str = "say") -> Message:
    return Message(role=role, content=content, kind=kind)


def test_the_prompt_history_drops_progress_keeps_the_card_and_alternates_roles() -> None:
    messages = [
        _msg("user", "Lagebericht?"),
        _msg("assistant", "Schau ich mir an."),
        _msg("assistant", "", kind="progress"),
        _msg("assistant", "**Summary:** all fine", kind="report"),
        _msg("assistant", "Fertig, alles ruhig."),
        _msg("user", "Und die Platten?"),
    ]
    history = history_pairs(messages)
    assert [r for r, _ in history] == ["user", "assistant", "user"]
    answer = history[1][1]
    # the hub's "looking now" line is not shown to the model (it would learn to open with it)
    assert answer.startswith(f"{REPORT_LEAD}\n**Summary:** all fine")
    assert answer.endswith("Fertig, alles ruhig.")
    # a continued answer stays a turn of its own
    kept = history_pairs(messages[:5], keep_last=True)
    assert kept[-1] == ("assistant", "Fertig, alles ruhig.") and len(kept) == 3
    assert kept[1][1].startswith(REPORT_LEAD)


# --- through the chat --------------------------------------------------------------------------


def _kinds(client: TestClient, conv: str) -> list[tuple[str, str]]:
    messages = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    return [(m["role"], m["kind"]) for m in messages]


def test_a_finding_request_is_confirmation_card_and_an_answer(client: TestClient, world) -> None:
    _, _, model = world
    script_check(client)
    model.stream_text_fn = lambda body: LONG_CLOSING
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "Was ist mit web los?", "on")
    assert status == 200
    kinds = [e for e, _ in events]
    # the order a reader sees: meta, the confirmation, the progress line while tools run, the
    # result (line gone), the card, then the closing text
    order = [
        kinds.index(k)
        for k in (
            "meta",
            "bubble",
            "tools_start",
            "progress",
            "tools_done",
            "progress_done",
            "report",
        )
    ]
    assert order == sorted(order) and kinds.index("report") < kinds.index("delta")
    assert kinds[-1] == "done"
    bubble = next(d for e, d in events if e == "bubble")["message"]
    assert bubble["content"] == CONFIRMATIONS["de"] and bubble["kind"] == "say"
    progress = [d for e, d in events if e == "progress"]
    assert (
        progress[0]["text"] == ""
        and progress[-1]["text"] == "docker.logs · vm-docker — see why web restarts"
    )

    card = next(d for e, d in events if e == "report")["message"]
    assert card["kind"] == "report" and card["status"] == "complete"
    assert card["report"]["summary"] == "web ran out of memory."
    assert [f["evidence"] for f in card["report"]["findings"]] == [["E1"]]
    assert card["report"]["assumptions"][0]["claim"] == "the limit may be too low"
    assert card["report"]["proposal"]["text"] == "raise the memory limit"
    assert card["report"]["evidence"][0]["excerpt"].endswith("out of memory\n")
    assert card["agent_task_id"] and card["content"].startswith("**Summary:**")

    # stored: the question, the three assistant messages in this order, no progress line left
    assert _kinds(client, conv) == [
        ("user", "say"),
        ("assistant", "say"),
        ("assistant", "report"),
        ("assistant", "say"),
    ]
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert stored[1]["content"] == CONFIRMATIONS["de"]
    assert stored[3]["content"] == LONG_CLOSING
    done = events[-1][1]["message"]
    assert done["id"] == stored[3]["id"] and done["content"] == stored[3]["content"]

    # the model was told not to repeat the card, and was left room for a full answer
    sent = sent_messages(model)
    assert (
        "do NOT copy it" in sent[0]["content"] and "[live E1] web was killed" in sent[-1]["content"]
    )
    from tests.test_chat import chat_requests

    assert chat_requests(model)[-1]["max_tokens"] == chat.CLOSING_MAX_TOKENS


def test_a_plain_question_stays_one_bubble(client: TestClient, world) -> None:
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "Erkläre mir kurz, was ein Container ist", "off")
    assert status == 200
    kinds = [e for e, _ in events]
    assert "bubble" not in kinds and "report" not in kinds and "progress" not in kinds
    assert _kinds(client, conv) == [("user", "say"), ("assistant", "say")]


def test_a_follow_up_is_answered_with_the_card_in_the_prompt(client: TestClient, world) -> None:
    _, _, model = world
    script_check(client)
    conv = new_conv(client)
    ask_with(client, conv, "Was ist mit web los?", "on")
    ask_with(client, conv, "Danke, erklär mir bitte den Begriff OOM", "off")
    sent = sent_messages(model)
    roles = [m["role"] for m in sent if m["role"] != "system"]
    assert roles == ["user", "assistant", "user"]  # one assistant turn for the three messages
    assistant = next(m["content"] for m in sent if m["role"] == "assistant")
    assert assistant.startswith(REPORT_LEAD) and CONFIRMATIONS["de"] not in assistant
    assert "web was killed for memory" in assistant


def test_regenerate_replaces_the_whole_group(client: TestClient, world) -> None:
    script_check(client)
    conv = new_conv(client)
    ask_with(client, conv, "Was ist mit web los?", "on")
    before = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert len(before) == 4
    script_check(client)
    r = client.post(f"/api/conversations/{conv}/regenerate?tools=on", headers=AUTH)
    assert r.status_code == 200
    after = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [(m["role"], m["kind"]) for m in after] == [
        ("user", "say"),
        ("assistant", "say"),
        ("assistant", "report"),
        ("assistant", "say"),
    ]
    assert after[0]["id"] == before[0]["id"] and after[1]["id"] > before[3]["id"]


def test_the_migration_marks_everything_that_exists_as_say(client: TestClient, world) -> None:
    """The column default: a message stored without a kind is a say (existing rows stay so)."""
    conv = new_conv(client)
    ask_with(client, conv, "Hallo", "off")
    messages = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert {m["kind"] for m in messages} == {"say"} and all(m["report"] is None for m in messages)


def test_stopping_during_the_check_leaves_the_confirmation_and_no_progress_line(
    client: TestClient,
    world,
) -> None:
    import json as _json
    import threading

    from tests.fakenode import eventually
    from tests.test_agent import BLOCK

    look = _json.dumps(
        {
            "action": "tool",
            "node": "vm-docker",
            "capability": "docker.logs",
            "input": {"container": "web", "lines": 50},
            "reason": "see why web restarts",
        }
    )
    model = script_check(client)
    model.replies[:] = [look, BLOCK]  # the second answer of the agent never comes
    conv = new_conv(client)
    got: list[Any] = []
    t = threading.Thread(
        target=lambda: got.append(ask_with(client, conv, "Was ist mit web los?", "on"))
    )
    t.start()

    def progress_visible() -> None:
        messages = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
        assert any(m["kind"] == "progress" for m in messages)

    eventually(progress_visible, timeout=10)
    stopped = client.post(f"/api/conversations/{conv}/stop", headers=AUTH)
    model.released.set()
    t.join(timeout=15)
    assert stopped.status_code == 200
    messages = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [(m["role"], m["kind"]) for m in messages] == [("user", "say"), ("assistant", "say")]
    assert messages[1]["content"] == CONFIRMATIONS["de"] and messages[1]["status"] == "complete"


def test_a_progress_line_left_by_a_killed_hub_is_not_a_running_tool_at_the_next_start(
    client: TestClient,
    world,
    clean_db: str,
) -> None:
    from sqlalchemy import create_engine, text

    from topcoat_hub.chat_runs import sweep_interrupted

    conv = new_conv(client)
    engine = create_engine(clean_db)
    with engine.begin() as conn:
        user = conn.execute(
            text("SELECT user_id FROM conversations WHERE id = :c"), {"c": conv}
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO messages (conversation_id, user_id, role, content, status, kind) "
                "VALUES (:c, :u, 'assistant', 'docker.logs · vm-docker', 'running', 'progress')"
            ),
            {"c": conv, "u": user},
        )
    engine.dispose()
    client.portal.call(sweep_interrupted, client.app.state.sessionmaker)
    messages = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [(m["kind"], m["status"], m["content"]) for m in messages] == [
        ("say", "interrupted", "")
    ]


def test_a_call_that_gave_no_result_is_on_the_card_and_in_the_answers_prompt() -> None:
    from types import SimpleNamespace

    from topcoat_hub import chat_tools

    failure = {
        "capability": "web.fetch",
        "node": "vm-docker",
        "target": "https://example.org/produkt",
        "code": "http_404",
        "message": "page not found (404)",
    }
    task = _task(diagnosis={"summary": "s", "findings": [], "notes": [], "failed_calls": [failure]})
    card = report.build(task, [])
    assert card is not None and card["failures"] == [failure]
    assert "**Not retrieved:** https://example.org/produkt: page not found (404)" in report.text(
        card
    )
    block = chat_tools.check_block(SimpleNamespace(task=task, error="", evidence=[]), 4000)
    assert "NOT RETRIEVED" in block and "https://example.org/produkt: page not found" in block
    assert "general knowledge" in block


def test_a_research_question_is_not_asked_as_a_server_diagnosis() -> None:
    from topcoat_hub import chat_tools

    asked = chat_tools.task_prompt("Such im Web nach dem Preis der Karte", [("user", "x")])
    assert "research on the web" in asked and "no next step" in asked
    assert "research on the web" not in chat_tools.task_prompt(
        "Warum startet mein Container neu?", []
    )


def test_the_card_says_when_the_summary_rests_on_assumptions() -> None:
    card = report.build(_task(), [_ev("E1", "oom")])
    assert card is not None and card["summary_has_assumptions"] is True
    assert "rest on assumptions" in report.text(card)
    plain = report.build(
        _task(diagnosis={"summary": "s", "findings": [{"claim": "c", "tag": "live"}], "notes": []}),
        [],
    )
    assert plain is not None and plain["summary_has_assumptions"] is False


def _closing_requests(model: Any) -> list[dict[str, Any]]:
    from tests.test_chat import chat_requests

    return [r for r in chat_requests(model) if r.get("max_tokens") == chat.CLOSING_MAX_TOKENS]


def test_an_empty_closing_bubble_is_asked_for_again_once(client: TestClient, world) -> None:
    _, _, model = world
    script_check(client)
    answers = iter(["", "Fertig, web ist am Speicher gescheitert."])
    model.stream_text_fn = lambda body: next(answers)
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "Was ist mit web los?", "on")
    assert status == 200 and events[-1][0] == "done"
    assert _kinds(client, conv) == [
        ("user", "say"),
        ("assistant", "say"),
        ("assistant", "report"),
        ("assistant", "say"),
    ]
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert stored[3]["content"] == "Fertig, web ist am Speicher gescheitert."
    asked = _closing_requests(model)
    assert len(asked) == 2 and asked[1]["messages"][-1]["content"].startswith("Antworte jetzt")


def test_a_closing_bubble_that_stays_empty_is_written_by_the_hub_from_the_report(
    client: TestClient, world
) -> None:
    _, _, model = world
    script_check(client)
    model.stream_text_fn = lambda body: ""
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "Was ist mit web los?", "on")
    assert status == 200
    assert _kinds(client, conv) == [
        ("user", "say"),
        ("assistant", "say"),
        ("assistant", "report"),
        ("assistant", "say"),
    ]
    assert len(_closing_requests(model)) == 2  # asked once, then asked again exactly once
    last = events[-1][1]["message"]
    assert events[-1][0] == "done" and last["kind"] == "say"
    # the hub's own sentence: in the language of the question, from the card, never silence
    assert "Die Prüfung ist fertig." in last["content"]
    assert "web ran out of memory." in last["content"]
    assert "Bericht darüber" in last["content"]


def test_a_closing_fallback_says_what_stopped_the_check_in_the_users_language() -> None:
    from topcoat_hub import report

    card = {"incomplete": True, "incomplete_reason": "repeated_calls", "summary": "Zwei Dinge."}
    de = report.closing_fallback(card, "de")
    assert "nicht ganz fertig" in de and "die Abfragen wiederholten sich" in de
    assert "Zwei Dinge." in de
    en = report.closing_fallback({**card, "summary": ""}, "en")
    assert "the calls kept repeating" in en and "no result I can stand behind" in en
    assert report.closing_fallback(None, "de") == ""


def test_the_checks_task_names_the_language_of_the_question() -> None:
    from topcoat_hub import chat_tools

    de = chat_tools.task_prompt("lies Repo test und nenne mir den letzten Commit", [("user", "x")])
    assert "in German, the language of the question" in de
    en = chat_tools.task_prompt("Which containers are running?", [("user", "x")])
    assert "in English, the language of the question" in en
    # a question whose language is not clear gets no instruction
    assert "language of the question" not in chat_tools.task_prompt("Plex", [("user", "x")])


def test_after_a_check_only_notes_that_share_a_word_with_the_question_stay() -> None:
    from topcoat_hub.chat_engine.answer import notes_on_topic
    from topcoat_hub.memory import Recalled

    def note(text: str) -> Recalled:
        return Recalled(uuid.uuid4(), text, "", "active")

    stray = note("Der Nutzer wohnt in Geilenkirchen und hat Hamburg als Testeingaben verwendet.")
    fit = note("Das Repo test ist ein Testrepo für die Werkstatt.")
    asked = "Die letzte Commit-Nachricht im Repo test?"
    assert notes_on_topic([stray, fit], asked) == [fit]
    assert notes_on_topic([stray], "Wo wohne ich?") == [stray]  # about the user: all stay


def test_a_closing_answer_that_is_only_a_function_call_written_as_text_counts_as_empty() -> None:
    assert chat.only_tool_call_text(
        "<|tool_call>call:topcoat.repo.start({repo: 'test'})<tool_call|>"
    )
    assert not chat.only_tool_call_text("Der Branch steht. <|tool_call>x<tool_call|>")
    assert not chat.only_tool_call_text("")


def test_a_function_call_written_as_text_is_cut_from_the_end_of_an_answer() -> None:
    text = "Ich lese die Datei jetzt.\n\n<|tool_call>call:repo.read{path:'a.py'}<tool_call|>"
    assert chat.strip_tool_call_text(text) == "Ich lese die Datei jetzt."
    assert chat.strip_tool_call_text("Nichts zu entfernen.") == "Nichts zu entfernen."


def test_advice_under_a_report_that_no_evidence_holds_is_marked_not_checked(
    client: TestClient, world
) -> None:
    """#222: the report keeps findings and proposals apart; the answer under it named a menu path
    of another product that no tool result held. It reaches the person marked as not checked."""
    _, _, model = world
    script_check(client)
    model.stream_text_fn = lambda body: (
        "web ran out of memory. To log it, go to Settings → Client event logging → Log Events."
    )
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "What is wrong with web?", "on")
    assert status == 200 and events[-1][0] == "done"
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert stored[-1]["content"] == (
        "web ran out of memory. *Not checked:* To log it, go to Settings → Client event logging "
        "→ Log Events."
    )
    assert events[-1][1]["message"]["content"] == stored[-1]["content"]
    # the browser drops what streamed and shows the marked text
    kinds = [e for e, _ in events]
    assert "discard" in kinds and kinds.index("discard") > kinds.index("report")
    # and the model was told to mark such advice itself
    assert 'starts with "not checked:"' in sent_messages(model)[0]["content"]
