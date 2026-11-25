"""deploy/memory-reset.sh dumps the memory tables, then marks the notes deleted. A stand-in
``docker`` plays the database, so nothing real is touched."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "memory-reset.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "$*" in
  *pg_dump*) echo "FAKE-DUMP" ;;
  *pg_restore*) cat > /dev/null; [ -z "${FAKE_BAD_DUMP:-}" ] ;;
  *"select count(*)"*) echo 5 ;;
  *"update memory_items"*) printf '1\n1\n' ;;
  *psql*) echo "fake rows" ;;
  *) echo "unexpected docker call: $*" >&2; exit 99 ;;
esac
"""


@pytest.fixture
def box(tmp_path: Path) -> dict[str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    backups = tmp_path / "backup"
    return {"bin": bin_dir, "backups": backups, "log": tmp_path / "calls.log"}


def run(box: dict[str, Path], *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{box['bin']}:{os.environ['PATH']}",
        "TOPCOAT_BACKUP_DIR": str(box["backups"]),
        "FAKE_LOG": str(box["log"]),
        **extra,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )


def calls(box: dict[str, Path]) -> str:
    return box["log"].read_text() if box["log"].exists() else ""


def test_script_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_a_dry_run_counts_and_changes_nothing(box: dict[str, Path]) -> None:
    done = run(box, "--dry-run")
    assert done.returncode == 0, done.stderr
    assert "would delete: 5 notes" in done.stdout and "nothing was changed" in done.stdout
    assert "update memory_items" not in calls(box) and "pg_dump" not in calls(box)
    assert not box["backups"].exists()


def test_the_notes_are_deleted_only_after_a_dump_that_reads_back(box: dict[str, Path]) -> None:
    done = run(box)
    assert done.returncode == 0, done.stderr
    [dump] = list(box["backups"].glob("memory-before-reset-*.dump"))
    assert dump.read_text().strip() == "FAKE-DUMP" and oct(dump.stat().st_mode & 0o777) == "0o600"
    log = calls(box)
    assert log.index("pg_dump") < log.index("update memory_items")
    assert "kind not in ('rule', 'style')" in log  # the person's own rules and styles stay
    assert "notes marked deleted" in done.stdout


def test_a_dump_that_does_not_read_back_stops_before_any_change(box: dict[str, Path]) -> None:
    done = run(box, FAKE_BAD_DUMP="1")
    assert done.returncode == 3
    assert "update memory_items" not in calls(box)


def test_an_unknown_option_is_refused(box: dict[str, Path]) -> None:
    assert run(box, "--nope").returncode == 64
