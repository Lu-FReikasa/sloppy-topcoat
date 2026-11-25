# ruff: noqa: E501
"""``scripts/dev/make_kb_index.py`` (E3-04): the generator of the knowledge directory writes what
the hub's parser reads, skips what the knowledge base would not read, and is safe to run from cron."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from topcoat_hub import knowledge_directory as kdir

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "make_kb_index.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_kb_index", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "03_Server").mkdir()
    (tmp_path / "03_Server" / "node-docker.md").write_text(
        "# node-docker\n\nIntro.\n\n## Docker stack\n\nThe compose stack runs here.\n\n## Backups\n\nNightly.\n"
    )
    (tmp_path / "04_Dienste").mkdir()
    (tmp_path / "04_Dienste" / "kuma.md").write_text(
        "# Uptime Kuma\n\n## Setup\n\nKuma runs on node-docker behind the proxy.\n\n## Monitors\n\nList.\n"
    )
    (tmp_path / "SENSIBEL").mkdir()
    (tmp_path / "SENSIBEL" / "logins.md").write_text(
        "# node-docker\n\nadmin password: hunter2hunter2\n"
    )
    (tmp_path / "keys-password.md").write_text("# node-docker\n\nsecret\n")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    return tmp_path


def run(script: ModuleType, *args: str) -> int:
    return int(script.main(list(args)))


def test_names_mode_finds_every_section_that_mentions_a_host_or_service(
    script: ModuleType, root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    names = tmp_path_factory.mktemp("cfg") / "names.txt"  # not inside the notes: it is no note
    names.write_text("# hosts\nnode-docker: docker host, dock\nkuma\nnothing-here\n")
    assert run(script, "--root", str(root), "--names", str(names)) == 0
    entries = {e.name: e for e in kdir.parse((root / kdir.DIRECTORY_FILE).read_text())}
    assert set(entries) == {"node-docker", "kuma"}  # a name without a section: no entry
    docker = {(r.path, r.start, r.end) for r in entries["node-docker"].refs}
    assert ("03_Server/node-docker.md", 1, 11) in docker  # the file's own title section
    assert ("04_Dienste/kuma.md", 3, 6) in docker  # "Setup" mentions node-docker in its text
    assert entries["node-docker"].aliases == ["docker host", "dock"]
    assert all(
        "SENSIBEL" not in r.path and "password" not in r.path for r in entries["node-docker"].refs
    )
    # the lines are right: the hub cuts exactly this section from the note
    note = (root / "04_Dienste" / "kuma.md").read_text()
    ref = next(r for r in entries["kuma"].refs if r.label == "Setup")
    assert "Kuma runs on node-docker" in kdir.section(note, ref)[1]


def test_title_mode_uses_the_title_of_every_note_and_its_sections(
    script: ModuleType, root: Path
) -> None:
    assert run(script, "--root", str(root)) == 0
    entries = {e.name: e for e in kdir.parse((root / kdir.DIRECTORY_FILE).read_text())}
    assert "Uptime Kuma" in entries and "node-docker" in entries
    labels = {r.label for r in entries["Uptime Kuma"].refs}
    assert {"Setup", "Monitors"} <= labels
    # nothing the knowledge base would not read, and not the file itself
    paths = {r.path for e in entries.values() for r in e.refs}
    assert not any("SENSIBEL" in p or "password" in p or p.endswith(".png") for p in paths)
    assert kdir.DIRECTORY_FILE not in paths


def test_check_exits_1_when_the_file_would_change_and_0_when_it_is_current(
    script: ModuleType, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = root / kdir.DIRECTORY_FILE
    assert run(script, "--root", str(root), "--check") == 1 and not out.exists()  # nothing written
    assert run(script, "--root", str(root)) == 0
    first = out.read_text()
    assert run(script, "--root", str(root), "--check") == 0
    assert out.read_text() == first
    (root / "04_Dienste" / "kuma.md").write_text("# Uptime Kuma\n\n## New section\n\nx\n")
    assert run(script, "--root", str(root), "--check") == 1
    assert out.read_text() == first  # --check never writes
    assert run(script, "--root", str(root)) == 0
    assert "New section" in out.read_text()
    capsys.readouterr()


def test_stdout_prints_and_writes_nothing(
    script: ModuleType, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(script, "--root", str(root), "--stdout") == 0
    assert "## Uptime Kuma" in capsys.readouterr().out
    assert not (root / kdir.DIRECTORY_FILE).exists()


def test_a_run_leaves_no_temporary_file_and_a_custom_output_path_works(
    script: ModuleType, root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    elsewhere = tmp_path_factory.mktemp("out") / "dir.md"
    assert run(script, "--root", str(root), "--output", str(elsewhere)) == 0
    assert elsewhere.exists() and not (root / kdir.DIRECTORY_FILE).exists()
    assert not [p for p in root.rglob("*") if p.name.startswith(".") and p.is_file()]


def test_a_missing_root_is_an_error_not_a_traceback(script: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_:
        run(script, "--root", str(tmp_path / "nope"))
    assert exit_.value.code == 2  # argparse's usage error, no traceback
