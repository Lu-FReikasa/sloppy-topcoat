"""The development brief teaches the model the theme format without reading files: it must name
every value the schema allows, or the model invents the missing ones (live, 2026-11-10)."""

import json
import re

from topcoat_hub.application_brief import REFERENCES
from topcoat_hub.config import REPO_ROOT


def _enums(node):
    if isinstance(node, dict):
        if "enum" in node:
            yield from node["enum"]
        for v in node.values():
            yield from _enums(v)
    elif isinstance(node, list):
        for v in node:
            yield from _enums(v)


def test_dev_brief_names_every_value_of_the_theme_schema():
    schema = json.loads((REPO_ROOT / "proto/ui/theme.schema.json").read_text(encoding="utf-8"))
    text = REFERENCES["dev"]
    missing = sorted({v for v in _enums(schema) if not re.search(rf"\b{re.escape(v)}\b", text)})
    assert missing == []


def test_dev_brief_keeps_dark_themes_off_the_paper_base():
    """schreibtisch paints light paper windows whatever the colour variables say (live
    2026-11-11: every 'dark' theme came out bright with that base)."""
    text = REFERENCES["dev"]
    assert '"base": "terminal" (always' in text
    assert "never use it for a dark theme" in text
