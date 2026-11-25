"""The guard on a chat answer before it is stored (answer_guard), with the answers that went
wrong on the test hub: a tool call written as text, and a status claim with no check behind it."""

import pytest

from topcoat_hub import answer_guard
from topcoat_hub.chat_tools import check_need, topic_words


@pytest.mark.parametrize(
    "text",
    [
        "```bash repo.test_status```",
        "```bash\nrepo.test_status\n```",
        "[function=look_at_my_systems] [parameter=what] repo.test_status ]",
        "<tool_call>repo.branch</tool_call>",
    ],
)
def test_a_tool_call_written_as_text_is_not_an_answer(text: str) -> None:
    verdict = answer_guard.review(text, checked=False, language="de")
    assert verdict.reason == "tool_markup"
    assert "nichts ausgeführt" in verdict.text


def test_a_tool_call_is_replaced_in_english_too() -> None:
    verdict = answer_guard.review("[function=x] repo.test_status", checked=True, language="en")
    assert verdict.reason == "tool_markup"
    assert "Nothing was run" in verdict.text


def test_an_unchecked_status_claim_is_marked_not_dropped() -> None:
    text = "Ich habe den aktuellen Status des Tests geprüft. Der Test läuft noch."
    verdict = answer_guard.review(text, checked=False, language="de")
    assert verdict.reason == "unchecked_claim"
    assert verdict.text.startswith(text)
    assert "nicht geprüft" in verdict.text


def test_a_claim_backed_by_a_check_is_left_alone() -> None:
    text = "Der Test läuft noch, 25 Prozent, laut der Prüfung."
    assert answer_guard.review(text, checked=True, language="de").text == text


def test_a_plain_answer_is_left_alone() -> None:
    text = "Im Menü findest du den Chat, dort wählst du die Anwendung."
    verdict = answer_guard.review(text, checked=False, language="de")
    assert verdict == answer_guard.Review(text, "")


def test_the_real_check_question_now_asks_for_a_check() -> None:
    # the question that got an answer from memory on the test hub (27.10., 00:29)
    tools = topic_words(["repo.test_status", "repo.test_start", "github.read"])
    question = (
        "Bitte prüfe einmal, ob der Test, der durch unseren letzten Chat angestoßen wurde "
        "noch läuft und achte bitte darauf, ihn nicht aus versehen wieder zu starten"
    )
    assert check_need(question, tool_words=tools) == "check"
    assert check_need(question) == ""  # without the tools' words, nothing names a topic


def test_a_general_question_does_not_start_a_check() -> None:
    tools = topic_words(["repo.test_status"])
    assert check_need("Was ist ein Test in Python?", tool_words=tools) == ""
    assert check_need("Wie kann ich Docker installieren?", tool_words=tools) == ""


def test_topic_words_come_from_capability_names_without_the_plumbing() -> None:
    words = topic_words(["repo.test_status", "project.list", "web.search"])
    assert {"repo", "test", "status", "project", "search"} <= words
    assert "list" not in words and "web" not in words  # short or generic: not a topic


def test_a_guess_after_a_check_without_evidence_is_marked() -> None:
    # live 2026-11-09, the audio check: a cause nobody looked at, next to backed lines
    text = (
        "- Der Prozess audiodg.exe hat das HDAUDIO-Gerät entfernt [E2, E7].\n"
        "- Da der Fehler direkt mit dem Audioprozess verknüpft ist, könnte ein Treiberkonflikt "
        "oder eine fehlerhafte Energieverwaltung vorliegen.\n\n"
        "Soll ich die Details des Geräts genauer untersuchen?"
    )
    verdict = answer_guard.review(text, checked=True, language="de")
    assert verdict.reason == "unbacked_advice"
    assert verdict.text.startswith(text)
    assert "Nicht geprüft" in verdict.text and "Energieverwaltung" in verdict.text
    assert "audiodg" not in verdict.text.split("Nicht geprüft")[1]  # the backed line is not named


def test_a_guess_that_names_its_evidence_or_says_it_is_one_is_left_alone() -> None:
    for text in (
        "Wahrscheinlich ist der Treiber schuld [live E3].",
        "Hardware-Defekte könnten das Gerät trennen (aus dem Kopf, nicht geprüft).",
        "Soll ich prüfen, ob es wahrscheinlich am Treiber liegt?",
        "Der Dienst läuft.",
    ):
        assert answer_guard.review(text, checked=True, language="de").reason == "", text
    # without a check this rule does not apply (general advice in a plain chat is normal)
    plain = "Du solltest den Treiber aktualisieren."
    assert answer_guard.review(plain, checked=False, language="de").reason == ""


def test_an_answer_that_announces_a_look_after_the_check_says_nothing_runs() -> None:
    # live 2026-11-10: the run had read theme.ts, the answer only said it would read it now
    text = (
        "Ich schaue mir die Definition in `web/src/desktop/theme.ts` an, um das Schema zu "
        "verstehen, und versuche dann, ein Beispiel zu finden."
    )
    verdict = answer_guard.review(text, checked=True, language="de")
    assert verdict.reason == "announced_look" and "es läuft aber keine mehr" in verdict.text
    # "versuche" is no advice: the old note named this sentence a guess
    assert "Nicht geprüft" not in verdict.text
    offer = "Soll ich mir die Datei theme.ts jetzt genauer ansehen?"
    assert answer_guard.review(offer, checked=True, language="de").reason == ""
    told = "Die Datei `theme.ts` legt die eingebauten Themes fest [E4]."
    assert answer_guard.review(told, checked=True, language="de").reason == ""
