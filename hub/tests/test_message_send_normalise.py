"""The answer of a research run is put in shape before the schema check
(live refusals 2026-11-04..06)."""

import jsonschema

from topcoat_hub.message_tools import normalise_send, schema_for


def _valid(doc: dict) -> None:
    jsonschema.validate(doc, schema_for("research"))


def test_coverage_true_text_and_list_become_objects() -> None:
    doc = normalise_send(
        {
            "kind": "result",
            "summary": "s",
            "coverage": {"a": True, "b": "only partly", "c": ["x", "y"], "d": {"tag": "live"}},
        }
    )
    assert doc["coverage"] == {
        "a": {},
        "b": {"note": "only partly"},
        "c": {"note": "x; y"},
        "d": {"note": "tag: live"},
    }
    _valid(doc)


def test_coverage_as_a_list_of_points() -> None:
    doc = normalise_send({"kind": "result", "summary": "s", "coverage": ["Optionen", "Filter"]})
    assert doc["coverage"] == {"Optionen": {}, "Filter": {}}
    _valid(doc)


def test_claims_with_extra_keys_or_as_strings() -> None:
    doc = normalise_send(
        {
            "kind": "result",
            "summary": "s",
            "claims": [
                {"claim": "A", "evidence": ["E1"], "tag": "live"},
                "B",
                {"claim": "C", "evidence": "E2, E3"},
            ],
            "open_points": "one point",
        }
    )
    assert doc["claims"] == [
        {"claim": "A", "evidence": ["E1"]},
        {"claim": "B", "evidence": []},
        {"claim": "C", "evidence": ["E2", "E3"]},
    ]
    assert doc["open_points"] == ["one point"]
    _valid(doc)


def test_unreadable_values_stay_and_are_refused() -> None:
    doc = normalise_send({"kind": "result", "summary": "s", "claims": [42]})
    assert doc["claims"] == [42]
