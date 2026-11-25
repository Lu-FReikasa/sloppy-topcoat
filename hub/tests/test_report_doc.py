"""The report document of a thorough research (D102): what the code writes, and that nothing of it
is left to the model."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from topcoat_hub.report_doc import ReportInput, build_document, slug
from topcoat_hub.report_service import question_of, title_of

NOW = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)


def row(ref: str, url: str = "", excerpt: str = "", capability: str = "web.fetch", **inp: Any):
    source: dict[str, Any] = {
        "node": "web",
        "capability": capability,
        "at": "2026-11-04T05:01:02+00:00",
    }
    if url:
        source["url"] = url
    if inp:
        source["input"] = inp
    return SimpleNamespace(ref=ref, tag="web", source=source, excerpt=excerpt)


EVIDENCE = [
    row(
        "E1",
        "https://www.freedesktop.org/wiki/Software/systemd/",
        "systemd is a suite of basic building blocks for a Linux system.",
    ),
    row(
        "E2",
        "https://en.wikipedia.org/wiki/Systemd",
        "systemd was started by Lennart Poettering in 2010.",
    ),
    row("E3", capability="web.search", query="systemd history"),
]


def data(**over: Any) -> ReportInput:
    base = dict(
        title="Systemd und seine Rolle",
        question="Bitte schreibe einen Bericht über systemd.",
        language="de",
        depth="thorough",
        summary="Kurzfassung.",
        sections=[
            (
                "Vorwort",
                "Der Bericht behandelt systemd. Besonderes Augenmerk: die Geschichte [E2].",
            ),
            ("Geschichte", "# Anfang\nsystemd startete 2010 [E2]. Es ist eine Suite [E1, E2]."),
            ("Rolle", "systemd ist PID 1 [E1]."),
        ],
        claims=[
            {"claim": "systemd is a suite of basic building blocks", "evidence": ["E1"]},
            {"claim": "Es gibt 4000 Entwickler", "evidence": ["E9"]},
        ],
        evidence=EVIDENCE,
        failed=[{"capability": "web.fetch", "target": "https://example.org/x", "code": "timeout"}],
        stop_reason="no_progress",
        created_at=NOW,
        started_at=datetime(2026, 11, 4, 4, 0, tzinfo=UTC),
        finished_at=datetime(2026, 11, 4, 5, 5, 30, tzinfo=UTC),
        author_model="gemma-4-26B",
        author_node="node-nvidia",
        steps=31,
        tool_calls=18,
        models=[
            {
                "model": "gemma-4-26B",
                "node": "node-nvidia",
                "work": "agent",
                "calls": 12,
                "ms": 3_900_000,
                "tokens": 40000,
            }
        ],
        build="abc1234-overlay",
    )
    base.update(over)
    return ReportInput(**base)


def test_the_document_has_the_form_of_a_report() -> None:
    body, meta = build_document(data())
    assert body.startswith('---\ntitle: "Systemd und seine Rolle"')
    for part in (
        "# Systemd und seine Rolle",
        "**Fragestellung:** Bitte schreibe einen Bericht über systemd.",
        "## Inhalt",
        "## Vorwort",
        "### Grenzen dieser Recherche",
        "## 1. Geschichte",
        "## 2. Rolle",
        "## Kernaussagen mit Beleg",
        "## Quellenverzeichnis",
        "## Entstehung",
    ):
        assert part in body, part
    # the order of a paper: title, contents, foreword, chapters, evidence, sources, how it was made
    positions = [
        body.index(p)
        for p in (
            "## Inhalt",
            "## Vorwort",
            "## 1. Geschichte",
            "## Quellenverzeichnis",
            "## Entstehung",
        )
    ]
    assert positions == sorted(positions)
    assert meta["chapters"] == ["Geschichte", "Rolle"] and meta["depth"] == "thorough"


def test_the_contents_link_to_the_headings_of_the_document() -> None:
    body, _ = build_document(data())
    assert "- [Vorwort](#vorwort)" in body
    assert f"- [1. Geschichte](#{slug('1. Geschichte')})" in body
    assert slug("1. Geschichte") == "1-geschichte" and slug("Äußerer Rahmen!") == "äußerer-rahmen"
    assert slug("   ") == "section"


def test_the_marks_of_the_run_become_numbered_citations_in_order_of_appearance() -> None:
    body, _ = build_document(data())
    # E2 comes first (in the foreword), then E1
    assert "die Geschichte [1]." in body
    assert "systemd startete 2010 [1]. Es ist eine Suite [1, 2]." in body
    assert "systemd ist PID 1 [2]." in body
    assert "[E1]" not in body and "[E2]" not in body
    sources = body[body.index("## Quellenverzeichnis") :]
    assert sources.index("[1] https://en.wikipedia.org/wiki/Systemd") < sources.index(
        "[2] https://www.freedesktop.org"
    )
    assert "abgerufen 2026-11-04 05:01:02 UTC" in sources
    assert "Auszug: „systemd was started by Lennart Poettering in 2010.“" in sources


def test_the_headings_the_model_wrote_do_not_break_the_structure() -> None:
    body, _ = build_document(data())
    assert "### Anfang" in body and "\n# Anfang" not in body


def test_the_box_of_limits_is_written_by_the_code() -> None:
    body, meta = build_document(data())
    box = body[body.index("### Grenzen dieser Recherche") : body.index("## 1. Geschichte")]
    assert "Nicht erreicht: https://example.org/x (timeout)" in box
    assert "Ohne wörtlichen Beleg: Es gibt 4000 Entwickler" in box
    assert "Der Lauf endete vorzeitig" in box and "keine neue Quelle" in box
    assert meta["limits"] and meta["claims_total"] == 2 and meta["claims_backed"] == 1


def test_a_clean_run_says_so_without_claiming_completeness() -> None:
    body, meta = build_document(
        data(
            failed=[],
            stop_reason=None,
            claims=[{"claim": "systemd is a suite of basic building blocks", "evidence": ["E1"]}],
        )
    )
    box = body[body.index("### Grenzen dieser Recherche") : body.index("## 1. Geschichte")]
    assert "keine nicht erreichten Quellen" in box and "nicht, dass das Thema vollständig" in box
    assert meta["limits"] == []


def test_a_mark_the_run_never_made_is_named_not_hidden() -> None:
    body, meta = build_document(
        data(sections=[("Vorwort", "x"), ("Kapitel", "Das ist belegt [E7].")])
    )
    assert "[E7]" in body
    assert any("E7" in line for line in meta["limits"])


def test_the_key_claims_carry_their_literal_quote_and_source() -> None:
    body, _ = build_document(data())
    table = body[body.index("## Kernaussagen mit Beleg") : body.index("## Quellenverzeichnis")]
    assert "| systemd is a suite of basic building blocks |" in table
    assert "systemd is a suite of basic building blocks for a Linux system." in table


def test_how_it_was_made_is_a_table_of_facts() -> None:
    body, meta = build_document(data())
    origin = body[body.index("## Entstehung") :]
    for fact in (
        "| Dauer | 1 h 05 min |",
        "gemma-4-26B @ node-nvidia",
        "| Schritte | 31 |",
        "| Suchen | 1 |",
        "| Seiten gelesen | 2 |",
        "abc1234-overlay",
        "1 von 2 Kernaussagen",
    ):
        assert fact in origin or fact.replace("1 von 2", "von 2") in origin, fact
    assert "| gemma-4-26B | node-nvidia | agent | 12 | 1 h 05 min | 40000 |" in origin
    assert meta["seconds"] == 3930 and meta["steps"] == 31 and meta["models"][0]["calls"] == 12


def test_a_report_in_english_has_english_labels() -> None:
    body, _ = build_document(data(language="en", stop_reason=None, failed=[]))
    for part in (
        "**Question:**",
        "## Contents",
        "## Foreword",
        "### Limits of this research",
        "## Sources",
        "## How it was made",
    ):
        assert part in body, part


def test_without_chapters_the_summary_and_the_claims_are_the_result() -> None:
    body, _ = build_document(data(sections=[], summary="Alles Wichtige."))
    assert "## 1. Ergebnisse" in body and "Alles Wichtige." in body
    assert (
        "- systemd is a suite of basic building blocks [1]" in body.replace("[E1]", "[1]")
        or "[E1]" in body
    )
    assert "## Vorwort" in body  # the summary stands in for a foreword the model did not write


# ruff: noqa: E501, RUF001


def test_the_question_and_the_title_come_out_of_the_prompt_of_a_task() -> None:
    chat = "depth: thorough\nBitte arbeite einen Bericht über systemd aus. Danke.\n\n(Write the summary in German.)\n\n(The conversation so far, for context only:\nuser: x)"
    assert question_of(chat) == "Bitte arbeite einen Bericht über systemd aus. Danke."
    request = "[request from project in the project Shop]\nreply_to: n1\nchain: c1\ndepth: thorough\n\nHow does paging work?\n\nWanted:\n- depth: thorough\n\nWhen you are done, answer with research.message.send"
    assert question_of(request) == "How does paging work?"
    assert (
        title_of("Bitte arbeite einen Bericht über systemd aus. Danke.", "fallback")
        == "Bitte arbeite einen Bericht über systemd aus"
    )
    assert title_of("", "Fallback title") == "Fallback title"


# --- a run that ended early is documented as such ---------------------------------------------

STOPPED = "The run stopped (repeated_calls) before a diagnosis was written. The evidence collected so far is listed with the task; Continue starts a new run with fresh limits."


def stopped(**over: Any) -> ReportInput:
    base: dict[str, Any] = dict(
        sections=[],
        claims=[],
        summary=STOPPED,
        failed=[],
        stop_reason="repeated_calls",
        last_repeated="ls -a -d -1 -2 -F -H -H -H",
        calls=[
            {"kind": "notes", "target": "ls Befehl", "ok": True},
            {"kind": "search", "target": "ls command options Ubuntu", "ok": True},
            {"kind": "page", "target": "https://wiki.ubuntuusers.de/ls/", "ok": True},
            {"kind": "page", "target": "https://example.org/gone", "ok": False},
        ],
    )
    base.update(over)
    return data(**base)


def test_a_run_that_ended_early_says_so_at_the_top_in_plain_words() -> None:
    body, meta = build_document(stopped())
    assert "complete: false" in body and "| Stand | Entwurf, unvollständig, Version 1 |" in body
    banner = body[body.index("**Diese Recherche ist unvollständig.**") : body.index("## Inhalt")]
    assert (
        "vorzeitig beendet" in banner
        and "dieselbe Abfrage wurde mehrfach hintereinander gestellt" in banner
    )
    assert "zuletzt „ls -a -d -1 -2 -F -H -H -H“" in banner
    assert meta["complete"] is False and meta["end_reason"] == "repeated_calls"
    assert meta["last_repeated"].startswith("ls -a")


def test_the_placeholder_of_the_loop_is_not_printed_as_if_it_were_the_result() -> None:
    body, _ = build_document(stopped())
    assert "The run stopped (" not in body and "Continue starts a new run" not in body
    assert "Der Lauf hat kein eigenes Vorwort geschrieben" in body
    assert "Es liegt kein ausgearbeiteter Text vor." in body


def test_the_box_of_limits_names_the_reason_and_what_was_not_reached() -> None:
    body, _ = build_document(
        stopped(
            failed=[
                {"capability": "web.fetch", "target": "https://example.org/gone", "code": "timeout"}
            ]
        )
    )
    box = body[body.index("### Grenzen dieser Recherche") : body.index("## 1.")]
    assert "Der Lauf endete vorzeitig – dieselbe Abfrage wurde mehrfach" in box
    assert "Nicht erreicht: https://example.org/gone (timeout)" in box


def test_the_search_log_lists_every_request_with_its_result() -> None:
    body, meta = build_document(stopped())
    log = body[body.index("**Suchverlauf**") :]
    assert "| 1 | Notizen | ls Befehl | gelesen |" in log
    assert "| 2 | Suche | ls command options Ubuntu | gelesen |" in log
    assert "| 4 | Seite | https://example.org/gone | nicht erreicht |" in log
    assert meta["calls"] == 4


def test_what_the_model_wrote_after_the_stop_is_kept_and_marked_as_unchecked() -> None:
    from topcoat_hub.report_doc import WRITE_UP_KEY

    body, _ = build_document(
        stopped(sections=[(WRITE_UP_KEY, "# Bericht\n\nDie Optionen sind -l und -a.")])
    )
    chapter = body[
        body.index("## 1. Ergebnisse (Niederschrift des Modells)") : body.index("## Entstehung")
    ]
    assert (
        "nach dem Abbruch aus dem bis dahin Gelesenen verfasst" in chapter
        and "nicht auf Belege geprüft" in chapter
    )
    assert "Die Optionen sind -l und -a." in chapter and "\n# Bericht" not in chapter


def test_a_complete_run_has_no_banner_and_counts_what_it_read() -> None:
    body, meta = build_document(data(stop_reason=None, failed=[]))
    assert "complete: true" in body and "unvollständig" not in body.split("## Inhalt")[0]
    assert meta["complete"] is True
    assert "| Quellen | 2 zitiert, 2 gelesen |" in body and "| Hosts | 2 |" in body


def test_the_english_banner_and_reasons() -> None:
    body, _ = build_document(stopped(language="en", stop_reason="max_tool_calls", last_repeated=""))
    assert "**This research is incomplete.**" in body
    assert "the allowed number of tool calls was reached" in body
    assert "| Status | Draft, incomplete, Version 1 |" in body or "Draft, incomplete" in body


def test_the_service_reads_the_last_refused_repeat_and_the_log_of_the_calls() -> None:
    from topcoat_hub.report_service import call_log, last_repeated

    steps = [
        SimpleNamespace(
            kind="observation", data={"tool": {"code": "repeat", "input": {"query": "first"}}}
        ),
        SimpleNamespace(kind="assistant", data={}),
        SimpleNamespace(
            kind="observation", data={"tool": {"code": "repeat", "input": {"query": "second"}}}
        ),
        SimpleNamespace(kind="observation", data={"kind": "tool", "text": "x"}),
    ]
    assert last_repeated(steps) == "second" and last_repeated([]) == ""
    rows = [row("E1", "https://a.example/x", "t"), row("E2", capability="web.search", query="q")]
    rows[1].source["capability"] = "web.search"
    log = call_log(rows, [{"capability": "web.fetch", "target": "https://b.example/y"}])
    assert [(c["kind"], c["ok"]) for c in log] == [
        ("page", True),
        ("search", True),
        ("page", False),
    ]
    assert log[0]["target"] == "https://a.example/x" and log[1]["target"] == "q"


def test_the_tools_that_write_the_report_are_no_request_of_the_research() -> None:
    from topcoat_hub.report_service import call_log

    section = row("E2", capability="research.report.section", title="Befund", markdown="x [E1]")
    section.source["node"] = "messages"
    log = call_log([row("E1", "https://a.example/x", "t"), section], [])
    assert [c["kind"] for c in log] == ["page"]


def test_a_first_heading_that_repeats_the_chapter_title_is_dropped() -> None:
    from topcoat_hub.report_doc import _demote

    assert _demote("### Einleitung\n\nText.", "Einleitung").strip() == "Text."
    assert _demote("## **Vorwort:**\nText.", "Vorwort").strip() == "Text."
    # another heading, or the same words later in the text, stays (as a sub-heading)
    assert _demote("## Anderes\nText", "Einleitung") == "### Anderes\nText"
    assert "### Einleitung" in _demote("Text\n\n## Einleitung\nMehr", "Einleitung")


def test_the_answer_of_the_report_tool_is_no_source_and_a_sent_back_chapter_is_no_gap() -> None:
    own = SimpleNamespace(
        ref="E9",
        tag="live",
        source={"node": "messages", "capability": "research.report.section"},
        excerpt='{"sections":[{"title":"Vorwort","words":113}],"words":113}',
    )
    body, _ = build_document(
        data(
            evidence=[*EVIDENCE, own],
            sections=[
                ("Vorwort", "Kurz."),
                ("Kapitel", "Ein Satz, belegt [E1] und unbelegt [E9]."),
            ],
            claims=[{"claim": "Es gibt Abschnitte.", "evidence": ["E9"]}],
            failed=[{"capability": "research.report.section", "code": "needs_sources"}],
        )
    )
    assert '"sections"' not in body  # not shown as a quote, not in the list of sources
    assert "needs_sources" not in body


def test_a_chapter_title_the_model_numbered_is_not_numbered_twice() -> None:
    body, _ = build_document(
        data(sections=[("Vorwort", "V."), ("1. Optionen", "Text."), ("2) Filter", "Text.")])
    )
    assert "## 1. Optionen" in body and "## 2. Filter" in body
    assert "1. 1." not in body and "2. 2" not in body


def test_a_list_of_sources_the_model_wrote_in_a_chapter_is_dropped() -> None:
    from topcoat_hub.report_doc import _without_source_list

    text = (
        "Intro.\n\n### Zusammenfassung\nDas ist wichtig.\n\n### Quellenverzeichnis\n[1] x\n[2] y\n"
    )
    out = _without_source_list(text)
    assert "Quellenverzeichnis" not in out and "[2] y" not in out
    assert "Zusammenfassung" in out and "wichtig" in out
    # text after the list under a higher heading stays
    kept = _without_source_list("### Quellen\n[1] x\n\n## Ausblick\nWeiter.")
    assert kept.strip() == "## Ausblick\nWeiter."
