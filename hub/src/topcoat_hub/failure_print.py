"""The fingerprint of a failed fix (plan 2026-11-24, B4 "stop on the same failure", step 1.13).

Live 2026-11-23: three iterations of issue 2.19 failed the same way, and 2.7 got the same pytest
output eleven times. The same context gives the same answer, so a failure that repeats is not
worth another automatic draft. The fingerprint is the error class, the first red test and the
first error line, with numbers, ids and paths taken out (they change from run to run while the
failure stays the same). Two drafts in a row with the same fingerprint stop the automatic drafts
(issue_fixes.FixService.next_draft); the person decides.

Pure functions, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

# pytest's short summary: "FAILED hub/tests/test_a.py::test_b[1] - AssertionError: assert 1 == 2"
_SUMMARY = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$")
# the "E   ..." lines under a pytest failure
_E_LINE = re.compile(r"^E\s+(.+)$")
# vitest / tsc: "FAIL  src/a.test.tsx > panel > shows", "src/a.ts(3,4): error TS2304: …"
_WEB_FAIL = re.compile(r"^\s*(?:×|FAIL)\s+(\S+)(?:\s+>\s+(.*))?$")  # noqa: RUF001 (vitest)
_TS_ERROR = re.compile(r"error (TS\d+):\s*(.*)$")
_CLASS = re.compile(r"\b([A-Z]\w*(?:Error|Exception|Exit))\b")
# eslint: "  425:5  error  Calling setState synchronously …  react-hooks/set-state-in-effect"
_ESLINT = re.compile(r"^\s*\d+:\d+\s+error\s+(.*?)(?:\s{2,}([\w@/-]+))?$")
# any other line that says error (live 2026-11-25: an unparsed log printed only "tests ended")
_ANY_ERROR = re.compile(r"\berror\b", re.I)

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b(?:0x)?[0-9a-f]{6,}\b", re.I)
_PATH = re.compile(r"(?:[\w.-]*[/\\])+[\w.-]+|\b[\w-]+\.(?:py|tsx?|jsx?|json|css|md)\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_ADDRESS = re.compile(r"\bat 0x[0-9a-f]+\b", re.I)
MAX_PART = 160


def normalize(line: str) -> str:
    """A line of a failure without what changes between runs: paths, ids, numbers, spacing."""
    text = _ADDRESS.sub("at …", line)
    text = _UUID.sub("…", text)
    text = _PATH.sub("…", text)
    text = _HEX.sub("…", text)
    text = _NUMBER.sub("#", text)
    return " ".join(text.split())[:MAX_PART]


def _test_name(node_id: str) -> str:
    """``hub/tests/test_a.py::TestX::test_b[1]`` → ``test_a.py::TestX::test_b`` (the file's name
    stays, it says which test; the parameters go)."""
    head, sep, rest = node_id.partition("::")
    name = head.replace("\\", "/").rsplit("/", 1)[-1]
    return (name + sep + re.sub(r"\[.*\]$", "", rest))[:MAX_PART]


def _error_class(text: str) -> str:
    m = _CLASS.search(text)
    return m.group(1) if m else ""


def fingerprint(why: str, output: str = "") -> str:
    """``class | test | line`` of a failure. ``output`` is the test log (empty for a draft that was
    refused before any test: then ``why`` is what failed)."""
    test = line = cls = ""
    for raw in (output or "").splitlines():
        s = raw.rstrip()
        m = _SUMMARY.match(s)
        if m and not test:
            test = _test_name(m.group(1))
            if m.group(2):
                line = line or m.group(2)
            continue
        m = _E_LINE.match(s)
        if m and not line:
            line = m.group(1)
            continue
        m = _WEB_FAIL.match(s)
        if m and not test:
            test = _test_name(m.group(1)) + (f" > {m.group(2)}" if m.group(2) else "")
            continue
        m = _TS_ERROR.search(s)
        if m and not line:
            cls, line = cls or m.group(1), m.group(2)
            continue
        m = _ESLINT.match(s)
        if m and not line:
            cls, line = cls or m.group(2) or "eslint", m.group(1)
    if not test and not line:
        # a log in no known form: its first line that says error
        line = next((ln.strip() for ln in (output or "").splitlines() if _ANY_ERROR.search(ln)), "")
    if not test and not line:
        # no test named it: what the hub said (a refused draft, a workshop step that failed)
        line = next((ln for ln in (why or "").splitlines() if ln.strip()), "")
    cls = cls or _error_class(line) or _error_class(output or "")
    return " | ".join([cls, test, normalize(line)])


def of_fix(fix: Any) -> str:
    """The fingerprint of an ``IssueFix`` row (or anything with ``error`` and ``tests``)."""
    tests = getattr(fix, "tests", None) or {}
    return fingerprint(str(getattr(fix, "error", "") or ""), str(tests.get("output") or ""))


def repeats(prints: Sequence[str]) -> int:
    """How many of the newest ``prints`` (newest first) are the same failure, at least 1."""
    if not prints:
        return 0
    n = 1
    while n < len(prints) and prints[n] == prints[0]:
        n += 1
    return n


def spot_paths(paths: Iterable[str], why: str, output: str = "") -> list[str]:
    """The files of a draft the failure names (path or file name in the message or the log); all
    of them when it names none."""
    text = f"{why}\n{output}".replace("\\", "/")
    every = list(dict.fromkeys(p for p in paths if p))
    named = [p for p in every if _names(text, p)]
    return named or every


def _names(text: str, path: str) -> bool:
    """Whether ``text`` names the file: its path or its file name as a whole word (``x.py`` is
    not named by ``test_x.py``)."""
    name = path.rsplit("/", 1)[-1]
    return any(re.search(rf"(?<![\w.-]){re.escape(part)}(?![\w-])", text) for part in (path, name))


def at_spot(
    edits: Sequence[dict[str, Any]], files: Sequence[dict[str, Any]], spot: Iterable[str]
) -> set[tuple[str, ...]]:
    """What a draft writes into the files of ``spot``, comparable between drafts."""
    where = set(spot)
    out: set[tuple[str, ...]] = set()
    for e in edits:
        if e.get("path") in where:
            out.add(("edit", str(e["path"]), str(e.get("old") or ""), str(e.get("new") or "")))
    for f in files:
        if f.get("path") in where:
            out.add(("file", str(f["path"]), str(f.get("content") or "")))
    return out
