"""The named places of a file (code_symbols.py, plan step 1.10), on real files of the repo."""

from pathlib import Path

import pytest

from topcoat_hub import code_symbols as cs

HUB = Path(cs.__file__).resolve().parent
REPO = HUB.parents[2]  # hub/src/topcoat_hub -> the repository


def _read(rel: str) -> str:
    path = REPO / rel
    if not path.exists():
        pytest.skip(f"{rel} is not in this checkout")
    return path.read_text(encoding="utf-8")


def _one(path: str, text: str, name: str) -> cs.Symbol:
    found, near = cs.find(path, text, name)
    assert len(found) == 1, (name, found, near)
    return found[0]


def test_python_classes_methods_functions_and_imports() -> None:
    path = "hub/src/topcoat_hub/blocks/service.py"
    text = (HUB / "blocks/service.py").read_text(encoding="utf-8")
    ask = _one(path, text, "HubBlockPorts.ask")
    lines = text.splitlines()
    assert "async def ask(" in lines[ask.start] and ask.kind == "method"
    assert "return text, model" in "\n".join(lines[ask.start : ask.end])  # its whole body
    # as models write it: with def, parentheses, back quotes; the last part alone when it is one
    assert _one(path, text, "`async def HubBlockPorts.ask()`") == ask
    assert _one(path, text, "ask") == ask  # the bare name, when it is one symbol
    assert cs.find(path, text, "BlockService.ask")[0] == []  # another class: not taken
    imports = _one(path, text, "imports")
    assert lines[imports.start].startswith(("import", "from"))


def test_a_name_that_is_not_there_gives_the_nearest_names_not_lines() -> None:
    path = "hub/src/topcoat_hub/project_runs.py"
    text = (HUB / "project_runs.py").read_text(encoding="utf-8")
    found, near = cs.find(path, text, "RunService.finish_checklist")
    assert found == [] and "RunService._finish_checklist" in near and len(near) <= cs.NEAREST
    assert all(not n[:1].isdigit() for n in near)


def test_tsx_components_inner_functions_and_interfaces() -> None:
    path = "web/src/project/IterationPanel.tsx"
    text = _read(path)
    lines = text.splitlines()
    panel = _one(path, text, "IterationPanel")
    assert panel.kind == "component" and lines[panel.end - 1].startswith("}")
    iterate = _one(path, text, "IterationPanel.iterate")
    assert "async function iterate()" in lines[iterate.start]
    assert iterate.end < panel.end and lines[iterate.end - 1].strip() == "}"
    assert _one(path, text, "WORKSHOP_MODULE").lines == 1
    types = _read("web/src/project/types.ts")
    fix = _one("web/src/project/types.ts", types, "IssueFix")
    assert fix.kind == "interface" and "failure_print" in "\n".join(
        types.splitlines()[fix.start : fix.end]
    )


def test_json_key_paths_markdown_headings_css_rules() -> None:
    en = _read("web/src/i18n/en.json")
    key = _one("web/src/i18n/en.json", en, "issues.sameFailure")
    assert '"sameFailure"' in en.splitlines()[key.start]
    block = _one("web/src/i18n/en.json", en, "inbox.kind")
    assert block.lines > 5
    md = "# Title\n\nintro\n\n## Setup\n\nsteps\n\n### Detail\n\nx\n\n## Use\n\ny\n"
    setup = _one("docs/x.md", md, "Setup")
    assert setup.name == "## Setup" and "## Use" not in "\n".join(
        md.splitlines()[setup.start : setup.end]
    )
    css = ".a { color: red; }\n\n.project-fixes li {\n  margin: 0;\n}\n"
    rule = _one("web/src/x.css", css, ".project-fixes li")
    assert (rule.start, rule.end) == (2, 5)


def test_a_python_file_that_does_not_parse_still_has_its_defs() -> None:
    text = "class A:\n    def b(self):\n        return (\n\ndef c():\n    pass\n"
    assert [s.name for s in cs.symbols("hub/x.py", text) if s.kind != "imports"] == [
        "A",
        "A.b",
        "c",
    ]
