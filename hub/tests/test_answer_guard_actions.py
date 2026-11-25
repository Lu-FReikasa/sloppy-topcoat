"""The chat's answer must not claim an action the assistant cannot have done (2026-11-13)."""

from topcoat_hub import answer_guard


def test_a_claimed_hand_over_or_task_gets_the_note() -> None:
    for text in (
        "Okay, ich habe die Übergabe manuell abgeschlossen. Hier ist die Zusammenfassung.",
        "Ich habe eine Aufgabe im Projekt **Topcoat** angelegt, um deine Anfrage zu erfassen.",
        "I have sent the hand-over to Research.",
    ):
        r = answer_guard.review(text, checked=False, language="de")
        assert r.reason == "claimed_action", text
        assert "legt selbst nichts an" in r.text


def test_prepared_and_waiting_is_what_it_may_say() -> None:
    for text in (
        "Ich habe die Aufgabe vorbereitet; sie wartet auf deine Bestätigung.",
        "Ich habe dir eine Liste der Modelle erstellt: SDXL, AnimateDiff.",
        "Die Übergabe ist noch nicht abgeschickt.",
    ):
        assert answer_guard.review(text, checked=False, language="de").reason != "claimed_action", (
            text
        )


def test_a_button_written_as_text_is_taken_out() -> None:
    r = answer_guard.review(
        "Klicke unten.\n\n[Button: Aufgabe im Projekt „Misc“ speichern]",
        checked=True,
        language="de",
    )
    assert r.reason == "claimed_action"
    assert "[Button" not in r.text and "Klicke unten." in r.text
