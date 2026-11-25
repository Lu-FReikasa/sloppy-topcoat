"""The style sheet of a theme as parts (ui/theme_css.py)."""

from topcoat_hub.ui import theme_css

CSS = """--bg: #000;
--text: #eee;
/* the window */
.dt-program {
  border: 1px solid var(--text);
  background: url("data:image/svg+xml;utf8,<svg>{}</svg>");
}
@keyframes pop {
  from { transform: scale(.9); }
  to { transform: scale(1); }
}
.dt-task.dt-open,.dt-task.dt-front { color: red }
--late: 1px"""


def test_the_parts_come_in_order_with_their_kind() -> None:
    parts = theme_css.split(CSS)
    assert [(p.kind, p.name) for p in parts] == [
        ("decl", "--bg"),
        ("decl", "--text"),
        ("rule", ".dt-program"),
        ("at", "@keyframes pop"),
        ("rule", ".dt-task.dt-open, .dt-task.dt-front"),
        ("decl", "--late"),
    ]
    assert "<svg>{}</svg>" in parts[2].body  # braces in a string do not end the block
    assert "to { transform: scale(1); }" in parts[3].body


def test_join_keeps_every_part_and_split_reads_it_back() -> None:
    parts = theme_css.split(CSS)
    again = theme_css.split(theme_css.join(parts))
    assert [(p.kind, p.name) for p in again] == [
        ("decl", "--bg"),
        ("decl", "--text"),
        ("decl", "--late"),
        ("rule", ".dt-program"),
        ("at", "@keyframes pop"),
        ("rule", ".dt-task.dt-open, .dt-task.dt-front"),
    ]
    assert theme_css.declarations(again[3].body)[0] == ("border", "1px solid var(--text)")


def test_a_selector_has_one_spelling() -> None:
    assert theme_css.norm_selector(".a>.b ,\n .c") == ".a > .b, .c"
