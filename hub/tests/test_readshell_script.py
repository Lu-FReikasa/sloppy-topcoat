"""The read-only shell's setup script writes a systemd unit that real hosts reject if a listed
path is missing or written as a glob (systemd 255: status=226/NAMESPACE). Guard against it."""

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "readshell" / "setup-readshell.sh"


def test_script_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_unit_paths_are_optional_and_have_no_globs() -> None:
    text = SCRIPT.read_text()
    lines = [ln for ln in text.splitlines() if ln.startswith("InaccessiblePaths=")]
    assert lines, "no InaccessiblePaths line found"
    for line in lines:
        value = line.split("=", 1)[1]
        if value.startswith("$"):  # the drop-in; its paths are prefixed by the script
            continue
        for path in value.split():
            assert path.startswith("-/"), f"{path}: a missing path would stop the unit"
            assert not re.search(r"[*?\[]", path), f"{path}: systemd does not expand globs here"


def test_hide_paths_get_the_optional_prefix() -> None:
    text = SCRIPT.read_text()
    assert 'case $p in -*) ;; *) p="-$p" ;; esac' in text
