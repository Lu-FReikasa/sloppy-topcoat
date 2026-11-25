"""The class of a report (report_class.py, docs plan of 2026-11-09): public, sensitive, critical.

Scope: a secret makes a report critical and is never kept as a value; a host name, an address or a
known machine makes it sensitive; plain facts stay public; a suggestion can raise a class, never
lower it; a critical report passes only with an approval."""

from topcoat_hub.report_class import (
    CRITICAL,
    PUBLIC,
    SENSITIVE,
    classify,
    hide_secrets,
    may_pass,
    settle,
)


def test_a_secret_makes_a_report_critical_and_is_not_kept() -> None:
    text = "Der Zugang: password = hunter2-geheim, sonst nichts."
    assert classify(text) == CRITICAL
    assert "hunter2" not in hide_secrets(text)
    assert "[hidden]" in hide_secrets(text)


def test_a_host_name_or_an_address_makes_a_report_sensitive() -> None:
    assert classify("Der Host nas.home.arpa antwortet.") == SENSITIVE
    assert classify("Der Hub läuft unter 10.0.0.3.") == SENSITIVE
    assert classify("Der Dienst ist unter tools-host erreichbar.", ["tools-host"]) == SENSITIVE


def test_plain_facts_and_internet_sources_stay_public() -> None:
    assert classify("Die Dokumentation von Immich beschreibt die Speicherung.") == PUBLIC
    assert classify("Laut https://example.org/doc gilt die Regel.") == PUBLIC


def test_a_short_known_name_does_not_make_everything_sensitive() -> None:
    assert classify("Ein Pi ist klein.", ["pi"]) == PUBLIC


def test_a_suggestion_can_raise_a_class_but_never_lower_it() -> None:
    assert settle("public", SENSITIVE) == SENSITIVE
    assert settle("critical", PUBLIC) == CRITICAL
    assert settle("bogus", PUBLIC) == PUBLIC


def test_a_critical_report_passes_only_with_an_approval() -> None:
    assert may_pass(PUBLIC, approved=False)
    assert may_pass(SENSITIVE, approved=False)
    assert not may_pass(CRITICAL, approved=False)
    assert may_pass(CRITICAL, approved=True)


def test_a_result_with_a_secret_waits_for_a_person_not_for_review() -> None:
    from topcoat_hub.project_runs import secret_problem

    assert secret_problem([{"Befund": "ok", "Beleg": "E1"}]) is None
    assert secret_problem([{"Befund": "password = hunter2-geheim"}]) is not None
    assert secret_problem([{"Befund": "läuft auf 10.0.0.3"}]) is None  # sensitive passes


def test_a_dossier_with_a_secret_needs_an_approval_and_its_answer_is_held_back() -> None:
    from topcoat_hub.handover import HELD_BACK, dossier_class, summary_for_inbox

    plain = {"question": "Wie ist der Stand?", "context": [], "asked": "Stand", "withheld": []}
    secret = {"question": "Nimm das Passwort: hunter2-geheim", "context": [], "asked": "x"}
    assert dossier_class(plain) == PUBLIC
    assert dossier_class(secret) == CRITICAL
    assert may_pass(dossier_class(plain), approved=False)
    assert not may_pass(dossier_class(secret), approved=False)
    assert summary_for_inbox("Der Host läuft gut.") == "Der Host läuft gut."
    assert summary_for_inbox("token = abc123xyz") == HELD_BACK
    assert summary_for_inbox(None) is None


def test_a_result_class_is_read_from_its_table() -> None:
    from topcoat_hub.report_class import classify

    assert classify('[{"Befund": "ok", "Beleg": "E1"}]') == PUBLIC
    assert classify('[{"Befund": "läuft auf 10.0.0.3"}]') == SENSITIVE
