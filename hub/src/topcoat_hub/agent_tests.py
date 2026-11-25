"""Test runs of the dev workshop as the agent sees them (repo.test_start, repo.test_status).

A run goes on in the node after the call that started it, so "the tests passed" is only true when a
``repo.test_status`` in this task showed the state ``passed``. :class:`TestRuns` remembers what the
task has seen; :func:`unbacked_claim` is the hub's own check of the agent's answer against it: no
model, no guessing.
"""

import re
from dataclasses import dataclass, field
from typing import Any

START = "repo.test_start"
STATUS = "repo.test_status"
# the states of a finished run that are no test result: the run was ended, the tests did not finish
ENDED_WITHOUT_RESULT = ("timed_out_idle", "timed_out_cap", "canceled", "error", "lost")

# "all tests passed", "tests are green", "1556 passed", "Tests bestanden", "Testlauf erfolgreich" …
_SUCCESS = re.compile(
    r"(?:\b(?:tests?|pytest|test suite|suite|testlauf|testl[aä]ufe)\b[^.\n]{0,60}?"
    r"\b(?:pass(?:ed|es|ing)?|green|succe(?:ss|eded|ssful)|bestanden|gr[uü]n|erfolgreich|fehlerfrei)\b"
    r"|\b\d+\s+(?:passed|bestanden)\b)",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(?:not|no|never|n't|nicht|kein\w*|nie|fail\w*|fehl\w*)\b", re.IGNORECASE)
_COUNT = re.compile(r"\b(\d+)\s+(?:passed|bestanden)\b", re.IGNORECASE)
# "1484 Tests waren erfolgreich", "1484 passed": a number, not "the tests passed"
_COUNTED = re.compile(
    r"\b(\d+)\s+(?:tests?\s+)?(?:(?:wurden|waren|sind|were|have|has)\s+)*"
    r"(?:passed|pass|bestanden|erfolgreich|successful|succeeded)\b",
    re.IGNORECASE,
)


@dataclass
class TestRuns:
    """What one task has seen of test runs: started ones and the last state of each."""

    __test__ = False  # not a pytest class

    started: dict[str, str] = field(default_factory=dict)  # job_id -> repo
    last: dict[str, dict[str, Any]] = field(default_factory=dict)  # job_id -> last status

    def note(self, capability: str, output: Any) -> None:
        if not isinstance(output, dict) or not isinstance(output.get("job_id"), str):
            return
        job = output["job_id"]
        if capability == START:
            self.started[job] = str(output.get("repo") or "")
        elif capability == STATUS:
            self.last[job] = {
                "repo": str(output.get("repo") or ""),
                "state": str(output.get("state") or ""),
                "log": str(output.get("log") or ""),
            }

    def passed(self) -> list[dict[str, Any]]:
        return [r for r in self.last.values() if r["state"] == "passed"]


def state_of(output: Any) -> str:
    return str(output.get("state") or "") if isinstance(output, dict) else ""


def claims_success(text: str) -> bool:
    """The text says tests passed (a sentence that denies it - "did not pass", "kein Test
    bestanden" - does not count)."""
    for m in _SUCCESS.finditer(text):
        if not _NEGATION.search(text[max(0, m.start() - 14) : m.end()]):
            return True
    return False


def unbacked_claim(texts: list[str], runs: TestRuns) -> str:
    """Why the answer's statement about test results is not backed by what this task saw, or
    ``""`` when it says nothing about passing tests or what it says is backed."""
    # A failed run still has "1484 passed" in its summary: naming that number is a true statement.
    # Only the numbers of a seen run are taken out; anything else still has to be backed.
    seen_logs = "\n".join(r["log"] for r in runs.last.values())
    texts = [
        _COUNTED.sub(
            lambda m: "" if re.search(rf"\b{m.group(1)}\s+passed\b", seen_logs) else m.group(0), t
        )
        for t in texts
    ]
    said = [t for t in texts if t and claims_success(t)]
    if not said:
        return ""
    passed = runs.passed()
    if not passed:
        seen = "; ".join(
            f"run {job[:8]} was last seen as {r['state'] or 'unknown'}"
            for job, r in runs.last.items()
        )
        if not seen and runs.started:
            seen = "a run was started and never asked for"
        return (
            "Your answer says tests passed, but no repo.test_status in this task showed the state "
            "passed" + (f" ({seen})" if seen else " (no test run was seen at all)") + ". "
            "A run that is still running, was ended (timed_out_idle, timed_out_cap, canceled) or "
            "failed has no passing result. Ask repo.test_status first (wait_seconds 15) or say "
            "plainly that the result is not known."
        )
    logs = "\n".join(r["log"] for r in passed)
    for t in said:
        for n in _COUNT.findall(t):
            if not re.search(rf"\b{n}\s+passed\b", logs):
                return (
                    f"Your answer says {n} passed, but the output of the passing run does not "
                    f"show that number. Use the numbers of the run (repo.test_status) or leave "
                    "the number out."
                )
    return ""
