"""deploy/backup-hub.sh writes a checked dump and the TLS volume, and prunes by a retention rule.
A stand-in ``docker`` plays the database and the volume, so nothing real is touched."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "backup-hub.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
case "$*" in
  *pg_dump*) echo "FAKE-DUMP" ;;
  *pg_restore*) cat > /dev/null; [ -z "${FAKE_BAD_DUMP:-}" ] ;;
  "run "*) echo "FAKE-TAR" ;;
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
    env_file = tmp_path / "hub.env"
    env_file.write_text("TOPCOAT_IMAGE=sloppy-topcoat-hub:local-abc\n")
    backups = tmp_path / "backup"
    backups.mkdir()
    return {"bin": bin_dir, "env": env_file, "backups": backups}


def run(box: dict[str, Path], *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{box['bin']}:{os.environ['PATH']}",
        "TOPCOAT_ENV_FILE": str(box["env"]),
        "TOPCOAT_BACKUP_DIR": str(box["backups"]),
        **extra,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )


def touch_old(backups: Path, names: list[str]) -> None:
    """Files with falling modification times: the first name is the oldest."""
    for i, name in enumerate(names):
        path = backups / name
        path.write_text("old")
        os.utime(path, (1_000_000 + i, 1_000_000 + i))


def test_script_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_writes_dump_and_tls_archive(box: dict[str, Path]) -> None:
    done = run(box)
    assert done.returncode == 0, done.stderr
    names = sorted(p.name for p in box["backups"].iterdir())
    assert any(n.startswith("topcoat-nightly-") and n.endswith(".dump") for n in names)
    assert any(n.startswith("hub-tls-") and n.endswith(".tgz") for n in names)
    assert "last-backup.txt" in names
    assert not any(n.endswith(".part") for n in names)
    for path in box["backups"].glob("topcoat-nightly-*"):
        assert path.stat().st_mode & 0o077 == 0  # owner only: the archive holds the CA key


def test_an_unreadable_dump_is_not_kept(box: dict[str, Path]) -> None:
    done = run(box, FAKE_BAD_DUMP="1")
    assert done.returncode == 4
    assert list(box["backups"].iterdir()) == []


def test_retention_keeps_the_newest(box: dict[str, Path]) -> None:
    touch_old(box["backups"], [f"topcoat-nightly-2026010{i}-0330.dump" for i in range(1, 6)])
    touch_old(box["backups"], [f"topcoat-before-image-{i}.dump" for i in range(1, 5)])
    done = run(box, TOPCOAT_BACKUP_KEEP="3", TOPCOAT_BACKUP_KEEP_BEFORE="2")
    assert done.returncode == 0, done.stderr
    names = {p.name for p in box["backups"].iterdir()}
    nightly = sorted(n for n in names if n.startswith("topcoat-nightly-"))
    assert len(nightly) == 3  # the new one and the two newest old ones
    assert "topcoat-nightly-20260101-0330.dump" not in names
    assert {n for n in names if n.startswith("topcoat-before-")} == {
        "topcoat-before-image-3.dump",
        "topcoat-before-image-4.dump",
    }


def test_dry_run_changes_nothing(box: dict[str, Path]) -> None:
    touch_old(box["backups"], [f"topcoat-nightly-2026010{i}-0330.dump" for i in range(1, 4)])
    done = run(box, "--dry-run", TOPCOAT_BACKUP_KEEP="1")
    assert done.returncode == 0, done.stderr
    assert "would remove" in done.stdout
    assert len(list(box["backups"].iterdir())) == 3
