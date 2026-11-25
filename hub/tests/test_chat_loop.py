"""A conversation that goes round in circles, seen without a model (chat_loop.py; live 2026-11-17:
the same diagnosis twice, both ending with "Soll ich dir erklären, wie du das Logging
aktivierst?"), and the closing answer that must not claim what was not prepared."""

from types import SimpleNamespace

from topcoat_hub import chat_loop, chat_tools

OFFER = "Soll ich dir erklären, wie du das Logging aktivierst?"
FIRST = f"Das Pixel 7 ist am Lu AP. In den Logs stehen keine WLAN-Abbrüche. {OFFER}"


def test_the_same_offer_is_not_made_twice() -> None:
    again = (
        "In den Logs sind keine WLAN-Abbrüche zu finden. Soll ich dir erklären, wie du das "
        "Logging im Omada Controller aktivierst?"
    )
    assert chat_loop.without_repeated_offer(again, [FIRST]) == (
        "In den Logs sind keine WLAN-Abbrüche zu finden."
    )
    other = "Keine Abbrüche. Soll ich die Alerts der letzten Woche prüfen?"
    assert chat_loop.without_repeated_offer(other, [FIRST]) == other
    plain = "Keine Abbrüche gefunden."
    assert chat_loop.without_repeated_offer(plain, [FIRST]) == plain


def test_a_report_that_says_what_the_one_before_said_is_a_finding() -> None:
    old = (
        "**Summary:** Ich konnte keine WLAN-Probleme in den Logs von gestern finden. Das Google "
        "Pixel 7 ist aktuell stabil mit dem Lu AP verbunden.\n**Findings:** …"
    )
    new = (
        "**Summary:** Ich konnte in den Logs von gestern keine WLAN-Probleme für dein Google "
        "Pixel 7 finden. Das Gerät ist aktuell stabil mit dem Lu AP verbunden.\n**Findings:** …"
    )
    finding = chat_loop.repeated_result(new, old)
    assert finding is not None and finding["rule"] == "repeated_result"
    different = "**Summary:** So aktivierst du das Logging: Einstellungen, Site, Logs.\n"
    assert chat_loop.repeated_result(different, old) is None
    assert chat_loop.repeated_result("no card", old) is None


def _result(notes=(), failed=(), evidence=()):
    task = SimpleNamespace(diagnosis={"notes": list(notes), "failed_calls": list(failed)})
    ev = [SimpleNamespace(source={"capability": c}) for c in evidence]
    return chat_tools.CheckResult(task=task, evidence=ev)


def test_the_closing_answer_is_told_when_nothing_was_prepared() -> None:
    failed = [{"capability": "project.changeset.propose", "node": "project", "message": "kind"}]
    assert chat_tools.tried_to_write(_result(failed=failed), writing_application=False)
    assert chat_tools.tried_to_write(
        _result(notes=["a proposal was dropped: project.batch.propose …"]), False
    )
    assert chat_tools.tried_to_write(_result(), writing_application=True)
    assert not chat_tools.tried_to_write(_result(), writing_application=False)
    # a batch or plan that was prepared waits on its own card
    assert not chat_tools.tried_to_write(_result(evidence=["project.batch.propose"]), True)
    assert not chat_tools.tried_to_write(
        _result(notes=["the project.plan.propose of the finish was prepared"]), True
    )
    assert "Never say that something is ready" in chat_tools.NOTHING_PREPARED
