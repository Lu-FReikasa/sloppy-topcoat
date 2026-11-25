"""scripts/dev/release.sh: the order of the steps and that a dry run changes nothing. The script
itself talks to Docker and ssh; those parts are only printed here."""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "release.sh"


def run(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    full = {k: v for k, v in os.environ.items() if k != "TOPCOAT_PROD_SSH"} | env
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=full,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def tags() -> str:
    return subprocess.run(
        ["git", "tag", "--list", "backup/*"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def test_a_dry_run_lists_every_step_in_order_and_changes_nothing() -> None:
    before = tags()
    r = run("--dry-run", TOPCOAT_PROD_SSH="ops@prod.example.lan")
    out = r.stdout
    names = re.findall(r"step \d+ \((\w+)\)", out)
    assert names == ["tag", "check", "build", "smoke", "wayback", "transfer", "prod"], out
    assert "(dry run)" in out and "update-hub.sh --dry-run" in out
    assert tags() == before  # the backup tag is only announced
    # production is always updated through the script that dumps, checks and rolls back
    assert out.index("(transfer)") < out.index("(prod)")


def test_the_test_hub_is_gone() -> None:
    r = run("--dry-run", "--skip-test-hub", TOPCOAT_PROD_SSH="ops@prod.example.lan")
    assert r.returncode == 64 and "unknown option" in r.stderr
    assert "test hub" not in run("--dry-run", TOPCOAT_PROD_SSH="ops@prod.example.lan").stdout


def test_stop_after_ends_cleanly_and_the_remote_steps_need_a_server() -> None:
    r = run("--dry-run", "--stop-after", "build")
    assert r.returncode == 0 and "stopped after build" in r.stdout
    r = run("--dry-run", "--stop-after", "smoke")
    assert "(wayback)" not in r.stdout
    r = run("--dry-run")  # no TOPCOAT_PROD_SSH
    assert r.returncode == 64 and "TOPCOAT_PROD_SSH" in r.stderr


def test_without_a_main_ref_a_dry_run_goes_on_and_a_real_run_stops(tmp_path: Path) -> None:
    """A shallow checkout of a pull request has neither origin/main nor main: the dry run only
    announces the backup tag, a real run refuses (it could not mark what it would replace)."""
    (tmp_path / "scripts" / "dev").mkdir(parents=True)
    script = tmp_path / "scripts" / "dev" / "release.sh"
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    git = ["git", "-c", "user.email=ci@example.lan", "-c", "user.name=ci"]
    for args in (["init", "-q", "-b", "work"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run([*git, *args], cwd=tmp_path, check=True, capture_output=True)
    env = {k: v for k, v in os.environ.items() if k != "TOPCOAT_PROD_SSH"}

    def go(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), *args],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip

    dry = go("--dry-run", "--stop-after", "tag")
    assert dry.returncode == 0 and "stopped after tag" in dry.stdout, dry.stdout + dry.stderr
    real = go("--stop-after", "tag")
    assert real.returncode == 65 and "origin/main" in real.stderr
