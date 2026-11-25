"""Chat logic without I/O (E2-01): prompt building, context trimming, SSE reading."""

import json

from topcoat_hub import chat
from topcoat_hub.chat_store import title_from


def _hist(*pairs: tuple[str, str]) -> list[tuple[str, str]]:
    return list(pairs)


def test_prompt_budget_reserves_room_for_the_answer() -> None:
    assert chat.prompt_budget(8192) == 8192 - 2048
    assert chat.prompt_budget(1000) == 1000 - 256
    assert chat.prompt_budget(100) == 256  # never below the floor


def test_system_prompt_states_the_rules_and_only_the_real_capabilities() -> None:
    text = chat.system_prompt(None)
    assert "language of the user's last message" in text
    assert "code fences only for real code" in text and "never around ordinary text" in text
    # Nothing beyond text is available yet, and the model is told not to claim otherwise.
    assert "You can NOT" in text and "current information (weather, news" in text
    assert "remember other conversations" in text and "use tools" in text
    assert "Never claim, offer or pretend more" in text
    assert chat.system_prompt("  ") == text
    with_user = chat.system_prompt("Be brief.")
    assert with_user.startswith(text) and with_user.endswith("Be brief.")
    assert "Additional" not in with_user  # no boilerplate around the user's text


def test_system_prompt_lists_enabled_capabilities_instead_of_denying_them() -> None:
    caps = chat.Capabilities(memory=True, web_search=True, tools=("service status", "disk usage"))
    text = chat.system_prompt(None, None, caps)
    can, _, cannot = text.partition("You can NOT")
    assert "use your notes about the user" in can
    assert "search the web" in can
    assert "look at these with read-only tools: service status; disk usage" in can
    assert "current information" not in cannot  # web search is on
    assert "remember other conversations" not in cannot
    assert "read the user's files" in cannot  # knowledge is still off
    assert "use tools or access devices" not in cannot
    everything = chat.Capabilities(memory=True, knowledge=True, web_search=True, tools=("x",))
    assert "You can NOT" not in chat.system_prompt(None, None, everything)


def test_system_prompts_are_compact() -> None:
    full = chat.system_prompt(None)
    short = chat.system_prompt(None, None, None, compact=True)
    assert chat.estimate_tokens(full) <= 280 and chat.estimate_tokens(short) <= 100
    assert len(short) < len(full) / 2
    # the short form still says the essentials
    for needle in ("Text only", "can NOT", "use tools", "Never claim more", '"du"', "Markdown"):
        assert needle in short
    assert "formal)" in chat.system_prompt(None, "sie", compact=True)
    assert chat.system_prompt("Be brief.", compact=True).endswith("Be brief.")


def _story(n: int) -> str:
    return " ".join(f"wort{i}" for i in range(n)) + " Die zwe"


def test_continue_prompt_with_everything_fitting_has_the_full_text() -> None:
    hist = [("user", "erzähl"), ("assistant", "Es war einmal")]
    p = chat.build_continue_prompt("sys", hist, 2000)
    assert p is not None and p.trimmed == 0
    assert [m["role"] for m in p.messages] == ["system", "user", "assistant", "user"]
    assert p.messages[2]["content"] == "Es war einmal"
    assert p.messages[3]["content"] == chat.CONTINUE_INSTRUCTION


def test_continue_prompt_keeps_the_end_of_a_long_answer_and_drops_old_turns_first() -> None:
    story = _story(600)
    hist = [
        ("user", "alte frage " * 80),
        ("assistant", "alte antwort " * 80),
        ("user", "erzähl eine geschichte"),
        ("assistant", story),
    ]
    budget = 700
    p = chat.build_continue_prompt("sys", hist, budget)
    assert p is not None
    tail = p.messages[-2]["content"]
    assert p.messages[-2]["role"] == "assistant" and tail.endswith("Die zwe")
    assert tail.startswith("[…] ") and story.endswith(tail[4:])
    assert p.messages[-3] == {"role": "user", "content": "erzähl eine geschichte"}
    assert p.trimmed == 2  # the old turns went, not the story
    free = budget - chat.estimate_tokens("sys") - chat.estimate_tokens(chat.CONTINUE_INSTRUCTION)
    assert chat.estimate_tokens(tail) >= 0.4 * free
    assert p.estimated_tokens <= budget


def test_continue_prompt_cuts_a_huge_question_before_the_story_tail() -> None:
    hist = [("user", "frage " * 2000), ("assistant", _story(300))]
    p = chat.build_continue_prompt("sys", hist, 600)
    assert p is not None
    question, tail = p.messages[1]["content"], p.messages[2]["content"]
    assert question.endswith("[…]") and question.startswith("frage")
    assert tail.endswith("Die zwe")
    assert p.estimated_tokens <= 600


def test_continue_prompt_refuses_when_nothing_useful_fits() -> None:
    hist = [("user", "q"), ("assistant", _story(100))]
    assert chat.build_continue_prompt("s" * 3000, hist, 256) is None
    assert chat.build_continue_prompt("sys", hist, 256) is not None


def test_system_prompt_address_form() -> None:
    assert '"du" (informal)' in chat.system_prompt(None)
    assert '"du" (informal)' in chat.system_prompt(None, "du")
    formal = chat.system_prompt(None, "sie")
    assert '"Sie" (formal)' in formal and "informal" not in formal
    assert '"du" (informal)' in chat.system_prompt(None, "nonsense")


def test_everything_fits() -> None:
    p = chat.build_prompt("sys", _hist(("user", "a"), ("assistant", "b"), ("user", "c")), 10_000)
    assert [m["role"] for m in p.messages] == ["system", "user", "assistant", "user"]
    assert p.trimmed == 0
    assert p.messages[-1]["content"] == "c"


def test_oldest_turns_are_left_out_and_counted() -> None:
    long = "x" * 300  # 104 tokens
    history = _hist(
        ("user", long), ("assistant", long), ("user", long), ("assistant", long), ("user", "last")
    )
    p = chat.build_prompt("sys", history, budget=250)
    assert p.messages[-1]["content"] == "last"
    assert p.messages[0]["role"] == "system"
    assert p.trimmed == 5 - (len(p.messages) - 1)
    assert p.trimmed >= 2
    # the prompt starts with a user turn after the system message
    assert p.messages[1]["role"] == "user"
    assert p.estimated_tokens <= 250 + chat.estimate_tokens(long)


def test_newest_message_is_kept_even_if_it_alone_exceeds_the_budget() -> None:
    p = chat.build_prompt(
        "sys", _hist(("user", "old"), ("assistant", "a"), ("user", "y" * 5000)), 100
    )
    assert [m["role"] for m in p.messages] == ["system", "user"]
    assert p.messages[1]["content"] == "y" * 5000
    assert p.trimmed == 2


def test_unanswered_user_messages_are_merged_and_failed_answers_dropped() -> None:
    history = _hist(
        ("user", "first"),
        ("assistant", ""),
        ("user", "second"),
        ("assistant", "ok"),
        ("user", "third"),
    )
    p = chat.build_prompt("sys", history, 10_000)
    assert [(m["role"], m["content"]) for m in p.messages[1:]] == [
        ("user", "first\n\nsecond"),
        ("assistant", "ok"),
        ("user", "third"),
    ]
    assert p.trimmed == 0


def test_title_from_first_message() -> None:
    assert title_from("  Hallo   Welt\nzweite Zeile") == "Hallo Welt"
    assert title_from("x" * 100) == "x" * 60 + "…"


def _event(obj: dict | str) -> bytes:
    data = obj if isinstance(obj, str) else json.dumps(obj)
    return f"data: {data}\n\n".encode()


def test_sse_reader_text_usage_and_done() -> None:
    r = chat.SseReader()
    out = []
    out += r.feed(_event({"choices": [{"delta": {"content": "Hal"}}]}))
    out += r.feed(_event({"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]}))
    out += r.feed(_event({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    out += r.feed(_event({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}))
    out += r.feed(_event("[DONE]"))
    assert "".join(c.text for c in out) == "Hallo"
    assert [c.finished for c in out] == [False, False, True, False, False]
    assert out[3].prompt_tokens == 12 and out[3].completion_tokens == 3
    assert out[-1].done


def test_sse_reader_handles_split_events_crlf_and_multibyte() -> None:
    raw = _event({"choices": [{"delta": {"content": "Grüße"}}]}).replace(b"\n", b"\r\n")
    r = chat.SseReader()
    got = []
    for i in range(0, len(raw), 7):  # arbitrary chunk boundaries, also inside "ü"
        got += r.feed(raw[i : i + 7])
    assert [c.text for c in got] == ["Grüße"]


def test_sse_reader_llama_timings_error_and_garbage() -> None:
    r = chat.SseReader()
    got = r.feed(_event({"choices": [], "timings": {"prompt_n": 40, "predicted_n": 9}}))
    assert (got[0].prompt_tokens, got[0].completion_tokens) == (40, 9)
    got = r.feed(_event({"error": {"message": "context too long", "type": "x"}}))
    assert got[0].error == "context too long"
    assert r.feed(b": keepalive\n\n") == []
    assert r.feed(_event("not json")) == []
    assert r.feed(_event({"choices": [{"delta": {"content": None}}]}))[0].text == ""


# --- notes in the prompt: no machine-format markings a small model would copy -------------------


def test_notes_are_described_in_words_not_in_tags_and_counts() -> None:
    block = chat.notes_context(
        [
            ("doc", "active", "Hund heißt Bello", 5),
            ("assumption", "active", "Ist müde", 0),
            ("live", "active", "Hat ein Auto", 2),
            ("doc", "disputed", "Lebt in Berlin", 1),
        ]
    )
    assert "- Hund heißt Bello — the user said it, confirmed many times" in block
    assert "- Ist müde — only inferred, unsure" in block
    assert "- Hat ein Auto — checked, certain, confirmed several times" in block
    assert "- Lebt in Berlin — disputed: contradicts another note, not settled" in block
    for marking in ("[doc]", "[live]", "[assumption]", "(confirmed", "(disputed)", "5x"):
        assert marking not in block, marking
    assert chat.notes_context([]) == ""
    rules = chat.system_prompt(None, None, chat.Capabilities(memory=True))
    assert "never quote or repeat these remarks, tags or counts" in rules
    for marking in ("[doc]", "(confirmed", "(disputed)"):
        assert marking not in rules
    assert chat.note_hint("doc", "active", 1) == "the user said it, confirmed once"


def test_the_system_prompt_does_not_depend_on_the_question() -> None:
    """D27: the notes change with every question, so they are not in the system prompt (that
    would make the model process the whole prompt again each time)."""
    caps = chat.Capabilities(memory=True)
    assert "Notes about the user for this message" in chat.system_prompt(None, None, caps)
    # the same text with or without notes, nothing per-question in it
    assert chat.system_prompt("Be brief.", "du", caps) == chat.system_prompt(
        "Be brief.", "du", caps
    )
    assert "Notes about the user" not in chat.system_prompt(None, None, chat.Capabilities())
    compact = chat.system_prompt(None, None, caps, compact=True)
    assert "Notes about the user for this message" in compact
    assert len(compact) < len(chat.system_prompt(None, None, caps))


def test_the_notes_stand_in_the_last_user_turn_and_leave_the_prefix_alone() -> None:
    history = [("user", "Hi"), ("assistant", "Hello!"), ("user", "Wo wohne ich?")]
    system = chat.system_prompt(None, None, chat.Capabilities(memory=True))
    plain = chat.build_prompt(system, history, 100_000)
    block = chat.notes_context([("doc", "active", "Wohnt in Hamburg", 0)])
    noted = chat.build_prompt(system, history, 100_000, block)
    other = chat.build_prompt(
        system, history, 100_000, chat.notes_context([("doc", "active", "Hund heißt Bello", 0)])
    )
    # everything before the last user turn is byte-identical whatever the notes are
    assert noted.messages[:-1] == plain.messages[:-1] == other.messages[:-1]
    assert noted.messages[0]["content"] == system
    last = noted.messages[-1]
    assert last["role"] == "user" and last["content"] == block + "Wo wohne ich?"
    assert "Wohnt in Hamburg" not in noted.messages[0]["content"]
    # the notes count against the budget, but the newest message is always kept
    assert noted.estimated_tokens > plain.estimated_tokens
    tight = chat.build_prompt(system, history, 5, block)
    assert tight.messages[-1]["content"].endswith("Wo wohne ich?") and tight.trimmed == 2
    # an assistant turn is never given notes
    assert chat.build_prompt(system, [("user", "a"), ("assistant", "b")], 10_000, block).messages[
        -1
    ] == {"role": "assistant", "content": "b"}


def test_a_continued_answer_gets_the_notes_with_its_question() -> None:
    history = [("user", "Wo wohne ich?"), ("assistant", "Du wohnst in Ham")]
    system = chat.system_prompt(None, None, chat.Capabilities(memory=True))
    block = chat.notes_context([("doc", "active", "Wohnt in Hamburg", 0)])
    prompt = chat.build_continue_prompt(system, history, 100_000, block)
    assert prompt is not None
    assert prompt.messages[1] == {"role": "user", "content": block + "Wo wohne ich?"}
    assert prompt.messages[2] == {"role": "assistant", "content": "Du wohnst in Ham"}


def test_strip_markers_removes_the_internal_markings_only() -> None:
    assert (
        chat.strip_markers("Das ist gut zu wissen, [doc] (confirmed 5x) hast aber einen Hund.")
        == "Das ist gut zu wissen, hast aber einen Hund."
    )
    assert chat.strip_markers("A [live] b [assumption] c (disputed) d (confirmed 12x)") == (
        "A b c d"
    )
    ordinary = "Siehe [1] und (siehe oben), Array[doc_id], (confirmed by mail), f(x)"
    assert chat.strip_markers(ordinary) == ordinary


def test_marker_filter_holds_back_markers_split_over_chunks() -> None:
    answer = "Gut, [doc] (confirmed 5x) du hast einen Hund [live] und (disputed) Katze (ok)."
    expected = chat.strip_markers(answer)
    for size in (1, 2, 3, 5, 7, 100):
        flt = chat.MarkerFilter()
        out = "".join(flt.feed(answer[i : i + size]) for i in range(0, len(answer), size))
        assert out + flt.flush() == expected, size
    # a bracket that never closes is shown once the stream ends
    flt = chat.MarkerFilter()
    assert flt.feed("Preis (ca.") + flt.flush() == "Preis (ca."


def test_api_times_are_utc_whatever_the_zone_of_the_connection() -> None:
    """E3-07 D2: the browser converts; the hub always says UTC (a Tokyo session or a naive value
    from a test do not leak a different offset)."""
    from datetime import datetime, timedelta, timezone

    from topcoat_hub.api.chat import _iso

    tokyo = timezone(timedelta(hours=9))
    assert _iso(datetime(2026, 10, 2, 9, 30, tzinfo=tokyo)) == "2026-10-02T00:30:00+00:00"
    assert _iso(datetime(2026, 10, 2, 0, 30)) == "2026-10-02T00:30:00+00:00"
    assert _iso(None) is None
