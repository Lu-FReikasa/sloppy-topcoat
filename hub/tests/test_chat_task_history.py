"""What a check's task knows of the conversation (live 2026-11-14: the run lost the thread).

With the last four messages cut to 500 characters, the task list an answer had written one turn
before was gone, and "as we just discussed" sent the run searching the notes."""

from types import SimpleNamespace

from topcoat_hub import chat
from topcoat_hub.api.chat import CONFIRMATIONS, task_pairs
from topcoat_hub.chat_tools import (
    TASK_HISTORY_CHARS,
    conversation_for_task,
    report_gist,
    task_prompt,
)


def _msg(role: str, content: str, kind: str = "say") -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, kind=kind)


def test_a_long_answer_of_an_earlier_turn_reaches_the_task() -> None:
    plan = "1.5 Recherche: Format lesen. " + "Schritt. " * 80 + "1.7 Entwicklung: Theme bauen."
    history = [
        ("user", "Formuliere die Aufgaben 1.5 bis 1.7 aus."),
        ("assistant", plan),
        ("user", "Wie besprochen, bitte."),
        ("assistant", "Gern."),
        ("user", "ja gern"),
        ("assistant", "Ok."),
        ("user", "Reiche es jetzt ein."),
    ]
    text = task_prompt("Reiche es jetzt ein.", history)
    assert "1.5 Recherche: Format lesen." in text
    assert "Formuliere die Aufgaben 1.5 bis 1.7 aus." in text  # older than four messages
    assert "Reiche es jetzt ein.\n\n(" in text  # the question stays first (question_of)


def test_the_room_keeps_the_newest_messages() -> None:
    history = [("user", f"message {i} " + "x" * 1500) for i in range(12)] + [("user", "now")]
    shown = conversation_for_task(history)
    assert "message 11" in shown and "message 0 " not in shown
    assert "(earlier messages left out)" in shown
    assert len(shown) <= TASK_HISTORY_CHARS + 2100  # one line may exceed the room, no more


def test_a_report_card_is_only_its_summary_and_confirmations_drop_out() -> None:
    card = "**Summary:** Die Aufgaben sind knapp.\n**Findings:**\n- [live] lange Evidenz [E1]"
    pairs = task_pairs(
        [
            _msg("user", "Wie stehen die Aufgaben?"),
            _msg("assistant", CONFIRMATIONS["de"]),
            _msg("assistant", "läuft", kind="progress"),
            _msg("assistant", card, kind="report"),
            _msg("assistant", "Sie sind knapp; ich schlage Unteraufgaben vor."),
        ]
    )
    assert pairs == [
        ("user", "Wie stehen die Aufgaben?"),
        (
            "assistant",
            "[report card] Die Aufgaben sind knapp.\n"
            "Sie sind knapp; ich schlage Unteraufgaben vor.",
        ),
    ]
    assert report_gist("") == ""


def test_a_finding_from_the_chat_is_not_called_the_users_memory() -> None:
    block = chat.review_context(
        [
            {
                "claim": "1.5: research",
                "reason": "the user named dev",
                "evidence": {"chat": True, "text": "1.5 soll die Dev Anwendung"},
            },
            {"claim": "c", "reason": "r", "evidence": {"result": "E1", "text": '"body":""'}},
        ]
    )
    assert "(the user wrote: 1.5 soll die Dev Anwendung)" in block
    assert '(tool result E1 says: "body":"")' in block
    assert "the user's memory says" not in block
