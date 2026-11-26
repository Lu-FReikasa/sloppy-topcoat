"""The GUI translates the names the hub and the nodes send in English by a stable id (issue #11):
the titles of the block programs that ship and the descriptions of the capabilities of hub and
nodes. A new program or capability without its two catalogue entries would show up in English in a
German GUI; the web side falls back to the sent text, so this guard keeps the lists complete."""

import json
import re
from pathlib import Path

import pytest

from topcoat_hub import protocol
from topcoat_hub.blocks import builtin

ROOT = Path(__file__).resolve().parents[2]
I18N = ROOT / "web/src/i18n"
# What the node declares in its source (`name: "windows.system"`, `const Name = "hub.update"`,
# `func (speakCap) Name() string { return "voice.speak" }`):
# the hub's lists miss what it never switches on by role (Windows, browser, restarts, files).
NODE_DECLARATION = re.compile(
    r"^\s*(?:name:|(?:const\s+)?Name\s*=|func \(\w+\) Name\(\) string \{ return)\s*"
    r'"([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)"',
    re.MULTILINE,
)


def _node_capabilities() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "node/internal").rglob("*.go"):
        if not path.name.endswith("_test.go"):
            found.update(NODE_DECLARATION.findall(path.read_text(encoding="utf-8")))
    return {n for n in found if not n.startswith("fake.")}  # fakes of tests never reach a person


NODE_CAPABILITIES = _node_capabilities()
CAPABILITIES = sorted(
    {
        *protocol.TOOL_CAPABILITIES,
        *protocol.DEV_CAPABILITIES,
        *protocol.BUILD_CAPABILITIES,
        *NODE_CAPABILITIES,
    }
)


def _catalogue(lang: str) -> dict:
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("lang", ["en", "de"])
def test_every_shipped_program_has_a_title_in_the_catalogue(lang: str) -> None:
    titles = _catalogue(lang)["blocksPage"]["programs"]
    assert sorted(n for n in builtin.PROGRAMS if not titles.get(n)) == []


def test_the_node_source_scan_finds_the_capabilities() -> None:
    # the scan must not pass on nothing: the node declares far more than the hub's role lists
    assert {"windows.system", "browser.open", "docker.restart", "hub.update", "voice.speak"} <= (
        NODE_CAPABILITIES
    )


@pytest.mark.parametrize("lang", ["en", "de"])
def test_every_capability_has_a_text_in_the_catalogue(lang: str) -> None:
    texts = _catalogue(lang)["tools"]["cap"]
    missing = []
    for name in CAPABILITIES:
        group, action = name.split(".")
        if not texts.get(group, {}).get(action):
            missing.append(name)
    assert missing == []
