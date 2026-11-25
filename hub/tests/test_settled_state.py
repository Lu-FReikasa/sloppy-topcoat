"""How a message settles when its run ends (api/chat.py): a research job keeps ``running``, so the
chat does not flash "error" between the start line and the first progress line."""

from types import SimpleNamespace

from topcoat_hub.api.chat import settled_state


def turn(**over):
    base = dict(research_job=False, interrupted=False, continues=None)
    return SimpleNamespace(**(base | over))


def test_a_research_job_keeps_running_so_its_watcher_can_write_the_progress() -> None:
    job = turn(research_job=True)
    assert settled_state(job, "running", "Recherche läuft", stop_requested=False) is None


def test_an_ordinary_answer_that_is_still_running_ends_as_error() -> None:
    assert settled_state(turn(), "running", "text", stop_requested=False) == "error"


def test_a_stopped_or_interrupted_answer_keeps_its_reason() -> None:
    assert settled_state(turn(), "running", "text", stop_requested=True) == "stopped"
    assert settled_state(turn(interrupted=True), "running", "text", stop_requested=False) == (
        "interrupted"
    )


def test_a_message_that_is_not_running_keeps_its_state() -> None:
    assert settled_state(turn(), "complete", "text", stop_requested=False) is None


def test_a_continued_answer_that_gained_nothing_stays_continuable() -> None:
    before = SimpleNamespace(content="cut off")
    assert settled_state(turn(continues=before), "running", "cut off", stop_requested=False) == (
        "truncated"
    )
