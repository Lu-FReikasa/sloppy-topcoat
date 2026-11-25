"""The documentation pages (2026-11-13): a Zweck paragraph only from a file's first lines, and an
introduction written from the confirmed Zweck paragraphs."""

import json

from topcoat_hub import project_programs as programs
from topcoat_hub import project_revise


def src(ref: str, start: int) -> programs.Source:
    return programs.Source(ref, "tool", "code", {"input": {"path": "a.py", "start_line": start}})


def test_a_purpose_from_later_lines_becomes_the_flow() -> None:
    table = [
        {"Abschnitt": "Zweck", "Text": "a.py speichert Läufe.", "Beleg": "E1"},
        {"Abschnitt": "Zweck", "Text": "Die Funktion x startet.", "Beleg": "E2"},
        {"Abschnitt": "Zweck", "Text": "Beides.", "Beleg": "E1, E2"},
        {"Abschnitt": "Ablauf", "Text": "Dann y.", "Beleg": "E2"},
    ]
    out = programs.purpose_from_start(table, [src("E1", 1), src("E2", 151)])
    assert [r["Abschnitt"] for r in out] == ["Zweck", "Ablauf", "Zweck", "Ablauf"]


def test_the_introduction_carries_the_sources_of_what_it_used() -> None:
    rows = [
        {"Abschnitt": "Zweck", "Text": "a.py speichert.", "Beleg": "E1"},
        {"Abschnitt": "Zweck", "Text": "b.py liest.", "Beleg": "E4"},
    ]
    answer = json.dumps(
        {
            "Text": "Der Teil speichert und liest. `a.py` und `b.py` teilen sich das.",
            "Aus": [1, 2, 9],
        }
    )
    row = project_revise.parse_overview(answer, rows, programs.OVERVIEW)
    assert row is not None and row["Abschnitt"] == "Überblick" and row["Beleg"] == "E1, E4"
    assert (
        project_revise.parse_overview(json.dumps({"Text": "x", "Aus": []}), rows, "Überblick")
        is None
    )
    assert programs.section_name("überblick") == "Überblick"
    assert programs.DOC_ORDER[0] == "Überblick"


def test_notes_as_a_term_of_the_code_stay() -> None:
    table = [
        {
            "Abschnitt": "Begriffe",
            "Text": "`REVIEW_KINDS` definiert die Arten von Review-Notizen.",
            "Beleg": "E1",
        },
        {
            "Abschnitt": "Ablauf",
            "Text": "Das Gedächtnis speichert Notizen. Mehr ist nicht ersichtlich.",
            "Beleg": "E1",
        },
    ]
    kept, removed = programs.tidy_doc_rows(table)
    assert kept[0]["Text"].startswith("`REVIEW_KINDS`")
    assert kept[1]["Text"] == "Das Gedächtnis speichert Notizen."
    assert [r["Aussage"] for r in removed] == ["Mehr ist nicht ersichtlich."]


def test_a_value_the_cited_code_does_not_show_leaves_the_page() -> None:
    code = "MAX_ESTIMATE = 60 * 24 * 366 * 5\nMAX_PENDING = 10_000\nLIMIT = 0.5"
    text = (
        "`MAX_ESTIMATE` begrenzt auf 3.153.600 Minuten. `MAX_PENDING` erlaubt 10.000 "
        "Einträge. `LIMIT` steht auf 0,5. Die Funktion läuft 12 Mal."
    )
    table = [
        {"Abschnitt": "Einstellungen", "Beleg": "E1", "Text": text},
        {"Abschnitt": "Einstellungen", "Beleg": "E2", "Text": "`BIG` ist 3.153.600."},
    ]
    kept, removed = programs.check_values(table, {"E1": code, "E2": "BIG = 3153600"})
    assert [r["Aussage"] for r in removed] == ["`MAX_ESTIMATE` begrenzt auf 3.153.600 Minuten."]
    assert "3.153.600" in removed[0]["Grund"]
    assert "10.000" in kept[0]["Text"] and "0,5" in kept[0]["Text"] and "12 Mal" in kept[0]["Text"]
    assert kept[1]["Text"] == "`BIG` ist 3.153.600."
