"""What the chat says about a research job while it runs and when it ends (research_chat.py)."""

from types import SimpleNamespace

from topcoat_hub import research_chat


def fake_run(steps: list[dict], blocks: int = 4) -> SimpleNamespace:
    return SimpleNamespace(
        program={"stack": [{"b": "x"}] * blocks},
        result=SimpleNamespace(steps=steps),
    )


def test_the_progress_counts_top_level_blocks_and_names_the_one_running() -> None:
    steps = [
        {"event": "step", "path": "stack[0]", "b": "agent", "state": "start"},
        {"event": "pick", "path": "stack[0]", "model": "qwen", "node": "node-nvidia"},
        {"event": "step", "path": "stack[0]", "b": "agent", "state": "done"},
        {"event": "step", "path": "stack[1]", "b": "check", "state": "start"},
        {"event": "call", "path": "stack[1]", "model": "gemma", "node": "node-nvidia"},
    ]
    line = research_chat.progress_line(fake_run(steps), "de")
    assert line == "Recherche läuft: 1 von 4 Blöcken fertig. Jetzt: check (node-nvidia · gemma)"


def test_nested_blocks_do_not_count_as_done_top_level_blocks() -> None:
    steps = [
        {"event": "step", "path": "stack[2].stack[0]", "b": "revise", "state": "done"},
        {"event": "step", "path": "stack[2].stack[0]", "b": "revise", "state": "start"},
    ]
    assert research_chat.progress_line(fake_run(steps), "en").startswith(
        "Research running: 0 of 4 blocks done. Now: revise"
    )


def test_the_done_line_links_the_report_and_keeps_the_title_free_of_brackets() -> None:
    line = research_chat.done_line("[ls] options", "abc", "de")
    assert line == (
        "Recherche fertig. Der Bericht [„(ls) options“](#/library/report%3Aabc) "
        "liegt in der Ablage."
    )


def test_an_unknown_language_falls_back_to_english() -> None:
    assert research_chat.failed_line("boom", "fr") == "The research did not finish: boom"


def test_the_summary_note_carries_the_report_shortened_to_the_limit() -> None:
    body = "§" * (research_chat.SUMMARY_CHARS + 500)  # a character the note's own text does not use
    note = research_chat.summary_note("ls", body, "de")
    assert "Recherche „ls“ ist fertig" in note
    assert note.count("§") == research_chat.SUMMARY_CHARS
