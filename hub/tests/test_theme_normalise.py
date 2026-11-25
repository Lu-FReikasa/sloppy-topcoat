"""A theme in the shapes small models write is read in the schema's shape (live 2026-11-11)."""

from topcoat_hub.ui.themes import normalise, theme_problems

BASE = {
    "key": "neon-nacht",
    "name": "Neon",
    "base": "terminal",
    "bar": {"position": "top", "items": ["start", "programs", "clock"]},
    "chrome": {"buttons": ["close"], "titlebar": "top"},
    "motion": {"style": "fade", "ms": 120},
    "css": ".dt-root { --bg: #000; }",
}


def test_a_bare_layout_and_extra_keys_are_put_in_shape() -> None:
    doc = {**BASE, "layout": "tile", "motion": "fade"}
    fixed, notes = normalise(doc)
    assert fixed["layout"] == {"kind": "tile"} and fixed["motion"] == {"style": "fade", "ms": 150}
    assert theme_problems(fixed) == [] and len(notes) == 2
    fixed, notes = normalise({**BASE, "layout": {"kind": "tile", "columns": 1, "rows": 1}})
    assert fixed["layout"] == {"kind": "tile"} and "columns" in notes[0]


def test_a_correct_theme_is_left_alone_and_a_wrong_value_is_still_named() -> None:
    doc = {**BASE, "layout": {"kind": "tile"}}
    assert normalise(doc) == (doc, [])
    fixed, _ = normalise({**BASE, "layout": "spiral"})
    assert any("layout" in p for p in theme_problems(fixed))
