"""The GUI translates the names the hub sends in English by a stable id (issue #11): the titles of
the block programs that ship. A new program without its two catalogue entries would show up in
English in a German GUI; the web side falls back to the sent text, so this guard keeps the list
complete. The capability texts are checked by the web against ``proto/capabilities.json``
(issue #206, ``web/src/i18n/capabilities.test.ts``)."""

import json
from pathlib import Path

import pytest

from topcoat_hub.blocks import builtin

ROOT = Path(__file__).resolve().parents[2]
I18N = ROOT / "web/src/i18n"


def _catalogue(lang: str) -> dict:
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("lang", ["en", "de"])
def test_every_shipped_program_has_a_title_in_the_catalogue(lang: str) -> None:
    titles = _catalogue(lang)["blocksPage"]["programs"]
    assert sorted(n for n in builtin.PROGRAMS if not titles.get(n)) == []
