"""deploy/fix-overlay/hub-fix-overlay.sh (D104): a request in the spool is checked, applied to a
copy of the overlay and swapped in; a hub that does not answer gets the overlay before back. A
stand-in ``docker`` and ``curl`` play the hub, so nothing real is touched."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "fix-overlay" / "hub-fix-overlay.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
case "$*" in *pg_dump*) echo DUMP ;; esac
# the GUI's build: writes index.html into the directory mounted as /out, or fails on FAKE_BUILD_FAIL
if [ "${1:-}" = run ]; then
  [ -n "${FAKE_BUILD_FAIL:-}" ] && { echo "error TS2304: x"; exit 1; }
  for a in "$@"; do case "$a" in *:/out) echo built > "${a%:/out}/index.html" ;; esac; done
fi
"""
# the health check: answers unless FAKE_DOWN names the build that runs
FAKE_CURL = """#!/usr/bin/env bash
want="TOPCOAT_OVERLAY_BUILD=${FAKE_DOWN:-}"
[ -n "${FAKE_DOWN:-}" ] && grep -q "$want" "$FAKE_LOG.env" 2>/dev/null && exit 7
exit 0
"""

PATCH = """diff --git a/topcoat_hub/x.py b/topcoat_hub/x.py
--- a/topcoat_hub/x.py
+++ b/topcoat_hub/x.py
@@ -1 +1 @@
-a = 1
+a = 2
"""


@pytest.fixture
def box(tmp_path: Path) -> dict[str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # the script calls python3 as the host has it; the workshop's test image has only python3.12
    # (live 2026-11-18: every fix ended red on "python3: command not found")
    python = f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'
    for name, text in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("python3", python)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    top = tmp_path / "topcoat"
    (top / "deploy").mkdir(parents=True)
    (top / "overlay" / "topcoat_hub").mkdir(parents=True)
    (top / "overlay" / "topcoat_hub" / "x.py").write_text("a = 1\n")
    (top / "overlay" / "BUILD").write_text("643dde6-overlay migrations=abc\n")
    (top / "overlay" / "web-src" / "src").mkdir(parents=True)
    (top / "overlay" / "web-src" / "src" / "a.ts").write_text("export const a = 1;\n")
    (top / "overlay" / "web-src" / "package-lock.json").write_text("{}\n")
    (top / "overlay" / "web-dist").mkdir()
    (top / "overlay" / "web-dist" / "index.html").write_text("old\n")
    spool = top / "hub-spool"
    (spool / "request").mkdir(parents=True)
    return {"bin": bin_dir, "top": top, "spool": spool, "log": tmp_path / "docker.log"}


def queue(box: dict[str, Path], patch: str = PATCH, base: str = "643dde6abcde") -> str:
    rid = str(uuid.uuid4())
    req = box["spool"] / "request" / rid
    req.mkdir()
    (req / "fix.patch").write_text(patch)
    (req / "manifest.json").write_text(
        json.dumps({"id": rid, "label": "fix-1.9.3", "base_commit": base, "migrations": False})
    )
    (req / "READY").write_text("")
    return rid


def run(box: dict[str, Path], **extra: str) -> dict:
    # the fake docker cannot see the variable compose gets: a wrapper notes it for the fake curl
    wrapper = box["bin"] / "docker"
    wrapper.write_text(
        FAKE_DOCKER + 'echo "TOPCOAT_OVERLAY_BUILD=${TOPCOAT_OVERLAY_BUILD:-}" > "$FAKE_LOG.env"\n'
    )
    env = {
        **os.environ,
        "PATH": f"{box['bin']}:{os.environ['PATH']}",
        "TOPCOAT_DIR": str(box["top"]),
        "TOPCOAT_FIX_SPOOL": str(box["spool"]),
        "TOPCOAT_HEALTH_WAIT": "3",
        "FAKE_LOG": str(box["log"]),
        **extra,
    }
    res = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    assert res.returncode == 0, res.stderr
    return {"stderr": res.stderr}


def outcome(box: dict[str, Path], rid: str) -> dict:
    return json.loads((box["spool"] / "done" / f"{rid}.json").read_text())


def test_a_fitting_fix_goes_live_with_its_build_name(box: dict[str, Path]) -> None:
    rid = queue(box)
    run(box)
    out = outcome(box, rid)
    assert out["state"] == "live" and out["build"] == "643dde6-overlay+fix-1.9.3"
    assert (box["top"] / "overlay" / "topcoat_hub" / "x.py").read_text() == "a = 2\n"
    assert (
        (box["top"] / "overlay" / "BUILD")
        .read_text()
        .startswith("643dde6-overlay+fix-1.9.3 migrations=abc")
    )
    assert (box["top"] / "overlay.old" / "topcoat_hub" / "x.py").read_text() == "a = 1\n"
    assert not (box["spool"] / "request" / rid).exists()


def test_a_hub_that_does_not_answer_gets_the_overlay_before(box: dict[str, Path]) -> None:
    rid = queue(box)
    run(box, FAKE_DOWN="643dde6-overlay+fix-1.9.3")
    out = outcome(box, rid)
    assert out["state"] == "rolled_back" and out["build"] == "643dde6-overlay"
    assert (box["top"] / "overlay" / "topcoat_hub" / "x.py").read_text() == "a = 1\n"


@pytest.mark.parametrize(
    ("patch", "base", "why"),
    [
        (PATCH.replace("topcoat_hub/x.py", "../etc/x.py"), "643dde6", "outside the overlay"),
        (PATCH, "1111111", "the fix was made on"),
        (PATCH.replace("-a = 1", "-a = 9"), "643dde6", "does not fit"),
    ],
)
def test_a_request_that_does_not_fit_is_refused_and_nothing_changes(
    box: dict[str, Path], patch: str, base: str, why: str
) -> None:
    rid = queue(box, patch, base)
    run(box)
    out = outcome(box, rid)
    assert out["state"] == "refused" and why in out["message"]
    assert (box["top"] / "overlay" / "topcoat_hub" / "x.py").read_text() == "a = 1\n"
    assert not box["log"].exists() or "up -d" not in box["log"].read_text()


WEB_PATCH = """diff --git a/web-src/src/a.ts b/web-src/src/a.ts
--- a/web-src/src/a.ts
+++ b/web-src/src/a.ts
@@ -1 +1 @@
-export const a = 1;
+export const a = 2;
"""


def test_a_fix_of_the_gui_is_built_on_the_host_and_goes_live(box: dict[str, Path]) -> None:
    """2026-11-20: Topcoat fixes its own GUI; the host builds web-src into web-dist."""
    rid = queue(box, WEB_PATCH)
    run(box)
    out = outcome(box, rid)
    assert out["state"] == "live"
    overlay = box["top"] / "overlay"
    assert (overlay / "web-src" / "src" / "a.ts").read_text() == "export const a = 2;\n"
    assert (overlay / "web-dist" / "index.html").read_text() == "built\n"
    assert "docker run --rm" in box["log"].read_text()


def test_a_gui_that_does_not_build_is_refused_and_nothing_changes(box: dict[str, Path]) -> None:
    rid = queue(box, WEB_PATCH)
    run(box, FAKE_BUILD_FAIL="1")
    out = outcome(box, rid)
    assert out["state"] == "refused" and "did not build" in out["message"]
    assert "TS2304" in out["output"]
    overlay = box["top"] / "overlay"
    assert (overlay / "web-src" / "src" / "a.ts").read_text() == "export const a = 1;\n"
    assert (overlay / "web-dist" / "index.html").read_text() == "old\n"
    assert "up -d" not in box["log"].read_text()


def test_the_package_files_of_the_gui_are_out_of_reach(box: dict[str, Path]) -> None:
    rid = queue(box, WEB_PATCH.replace("web-src/src/a.ts", "web-src/package-lock.json"))
    run(box)
    assert "outside the overlay" in outcome(box, rid)["message"]
