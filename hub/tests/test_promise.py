"""Announced, not done (E7-02, D57): the end of an answer that says what it is about to do and then
does not. The detector is narrow on purpose; these cases pin both sides of the line."""

import pytest

from topcoat_hub.promise import announced_action


@pytest.mark.parametrize(
    "text",
    [
        "Gerne! Ich schaue mal nach den Logs.",
        "Ich prüfe das jetzt.",
        "Ich werde das jetzt für dich prüfen.",
        "Ich werde zuerst den Speicher ansehen:",
        "Alles klar, ich mache das gleich.",
        "Ich rufe jetzt die Container-Liste ab …",
        "Ich starte den Test nun.",
        "Einen Moment bitte.",
        "Ich schaue nach den Containern. Einen Moment bitte.",
        "Ich schaue nach den Containern. Danke!",
        "I'll check that now.",
        "Sure. Let me look at the logs.",
        "Let me check the disk usage:",
        "I'm going to run the command right away.",
        "One moment please.",
        "Das ist eine gute Frage. Ich werde das jetzt nachschlagen.",
    ],
)
def test_an_answer_that_ends_on_an_announcement_is_found(text: str) -> None:
    assert announced_action(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Das Problem ist der Speicher. Erhöhe das Limit auf 2 GB.",
        "Docker ist eine Containertechnik. Sie startet Prozesse isoliert.",
        # an offer, a condition or a question is not an announcement
        "Soll ich das jetzt prüfen?",
        "Wenn du willst, schaue ich nach den Logs.",
        "Falls du möchtest, prüfe ich das jetzt.",
        "Möchtest du, dass ich nachsehe?",
        "I can check the logs if you want.",
        "Would you like me to look at it now?",
        # an announcement followed by what it announced is an introduction
        "Ich werde dir jetzt die Schritte erklären:\n1. Öffne die Datei.\n2. Ändere den Wert auf 5 "
        "und speichere sie, danach startest du den Dienst neu und prüfst das Ergebnis.",
        # past tense: it was done
        "Ich habe jetzt nachgesehen: Der Container läuft.",
        "Ich habe die Logs geprüft. Es gibt keinen Fehler.",
        "I checked the logs. Nothing unusual.",
        # a result after the announcement
        "Ich schaue nach. Die Logs zeigen einen Fehler im Speicher: out of memory killed the web "
        "container twice today, at 07:59 and at 08:12.",
        # the first person is needed: this is advice, not an announcement
        "Du solltest zuerst die Datei öffnen und dann kurz nachsehen.",
    ],
)
def test_everything_else_passes(text: str) -> None:
    assert announced_action(text) is None


def test_the_sentence_that_announces_is_returned() -> None:
    text = "Das klingt nach einem Speicherproblem. Ich schaue mal nach den Logs."
    assert announced_action(text) == "Ich schaue mal nach den Logs."
    both = "Ich prüfe das jetzt. Einen Moment bitte."
    assert announced_action(both) == "Einen Moment bitte."  # the last sentence is itself one


def test_only_the_end_of_a_long_answer_is_read() -> None:
    long = ("Ich werde das jetzt prüfen. " + "Hier sind die Ergebnisse im Detail. " * 20).strip()
    assert announced_action(long) is None  # said at the start, followed by plenty
    assert announced_action(long + " Ich schaue noch einmal nach.") is not None


def test_markdown_bullets_do_not_hide_the_sentence() -> None:
    assert announced_action("Plan:\n- Ich prüfe das jetzt") is not None
