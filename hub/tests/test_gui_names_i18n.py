"""The GUI translates the names the hub and the nodes send in English by a stable id (issue #11):
the titles of the block programs that ship and the descriptions of the capabilities the hub knows.
A new program or capability without its two catalogue entries would show up in English in a German
GUI; the web side falls back to the sent text, so this guard keeps the lists complete."""

import json
from pathlib import Path

import pytest

from topcoat_hub import protocol
from topcoat_hub.blocks import builtin

I18N = Path(__file__).resolve().parents[2] / "web/src/i18n"
CAPABILITIES = sorted(
    {*protocol.TOOL_CAPABILITIES, *protocol.DEV_CAPABILITIES, *protocol.BUILD_CAPABILITIES}
)


def _catalogue(lang: str) -> dict:
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("lang", ["en", "de"])
def test_every_shipped_program_has_a_title_in_the_catalogue(lang: str) -> None:
    titles = _catalogue(lang)["blocksPage"]["programs"]
    assert sorted(n for n in builtin.PROGRAMS if not titles.get(n)) == []


@pytest.mark.parametrize("lang", ["en", "de"])
def test_every_capability_the_hub_knows_has_a_text_in_the_catalogue(lang: str) -> None:
    texts = _catalogue(lang)["tools"]["cap"]
    missing = []
    for name in CAPABILITIES:
        group, action = name.split(".")
        if not texts.get(group, {}).get(action):
            missing.append(name)
    assert missing == []
