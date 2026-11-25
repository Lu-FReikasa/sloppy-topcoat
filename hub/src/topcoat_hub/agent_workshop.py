"""Rules of the hub for work in the dev workshop, checked before a call goes to the node (no model).

The workshop keeps ONE scratch copy per repository, shared by every task. A small model that is
careless with it destroys work nobody can get back (nothing is committed until the user pushes):

* **no discarding on its own**: ``repo.reset`` and ``repo.start`` with ``discard`` throw away
  uncommitted changes. They are refused while the copy is known to hold changes (or the state is
  not known yet), unless the question itself asks for it.
* **a new branch is started**: a question that asks for a new work branch needs ``repo.start``
  before the first change, else the work lands on whatever branch the copy was on.
* **no blind overwrite**: ``repo.write`` on a file that exists needs a ``repo.read`` of it in the
  same task, else the old content is replaced without anyone having looked at it.
"""

import re
from dataclasses import dataclass, field
from typing import Any

CHANGES = frozenset({"repo.write", "repo.edit", "repo.apply_patch"})

_NEW_BRANCH = re.compile(
    r"\b(?:neue[nmrs]?|new)\s+(?:work\s*)?(?:branch|zweig)\b",
    re.IGNORECASE,
)
_MAY_DISCARD = re.compile(
    r"verwirf|verworfen|wegwerf|wegzuwerfen|zur[uü]cksetz|discard|throw\s+away|\breset\b|"
    r"von\s+vorn|fresh\s+start",
    re.IGNORECASE,
)


def _path(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    return text.strip().removeprefix("./")


@dataclass
class Workshop:
    """What one task knows of the scratch copies it works in."""

    asks_branch: bool = False
    may_discard: bool = False
    started: bool = False  # repo.start succeeded in this task
    changed: bool = False  # this task changed files since the copy was last clean
    clean: dict[str, bool] = field(default_factory=dict)  # repo -> last known state
    dirty_files: dict[str, list[str]] = field(default_factory=dict)  # repo -> changed files
    listed: dict[str, set[str]] = field(default_factory=dict)  # repo -> files repo.list showed
    touched: dict[str, set[str]] = field(default_factory=dict)  # repo -> read or written here

    def of_prompt(self, prompt: str) -> None:
        self.asks_branch = bool(_NEW_BRANCH.search(prompt))
        self.may_discard = bool(_MAY_DISCARD.search(prompt))

    def refusal(self, capability: str, call_input: Any, offered: set[str]) -> str:
        """Why the hub does not make this call, or ``""``."""
        if not capability.startswith("repo.") or not isinstance(call_input, dict):
            return ""
        repo = str(call_input.get("repo") or "")
        if capability == "repo.reset" or (
            capability == "repo.start" and call_input.get("discard") is True
        ):
            return self._discard(capability, repo, offered)
        if (
            capability in CHANGES
            and self.asks_branch
            and not self.started
            and "repo.start" in offered
        ):
            return (
                "The question asks for a NEW work branch. Call repo.start (repo, topic) "
                "before you change any file: otherwise the change lands on whatever branch "
                "the scratch copy is on."
            )
        if capability == "repo.write":
            path = _path(call_input.get("path"))
            if path in self.listed.get(repo, set()) and path not in self.touched.get(repo, set()):
                return (
                    f"{path} exists and you have not read it in this task. Read it with "
                    "repo.read first. Change a part of it with repo.edit; write the whole file "
                    "only when all of it is meant to be replaced."
                )
        return ""

    def _discard(self, capability: str, repo: str, offered: set[str]) -> str:
        if self.may_discard:
            return ""
        files = self.dirty_files.get(repo, [])
        if self.changed or self.clean.get(repo) is False:
            shown = ", ".join(files[:8]) or "files changed earlier"
            return (
                f"The scratch copy of {repo or 'the repository'} holds uncommitted work ({shown}). "
                f"{capability} would delete it for good, and the question does not ask for that. "
                "Do not discard it: finish and tell the user the copy holds unsaved work, then "
                "ask whether it may be thrown away."
            )
        if self.clean.get(repo) is None and offered & {"repo.status", "repo.sync"}:
            return (
                f"Look before you discard: call repo.status for {repo or 'the repository'} first. "
                f"{capability} deletes uncommitted work and nothing in this task showed that the "
                "copy is clean."
            )
        return ""

    def note(self, capability: str, call_input: Any, output: Any) -> None:
        """Learn from a call that succeeded."""
        if not capability.startswith("repo.") or not isinstance(call_input, dict):
            return
        repo = str(call_input.get("repo") or "")
        path = _path(call_input.get("path"))
        out = output if isinstance(output, dict) else {}
        if capability == "repo.list":
            files = out.get("files")
            if isinstance(files, list):
                self.listed.setdefault(repo, set()).update(f for f in files if isinstance(f, str))
        elif capability == "repo.read" and path:
            self.touched.setdefault(repo, set()).add(path)
        elif capability in CHANGES:
            self.changed = True
            self.clean[repo] = False
            if path:
                self.touched.setdefault(repo, set()).add(path)
                self.listed.setdefault(repo, set()).add(path)
        elif capability in ("repo.start", "repo.reset"):
            self.clean[repo] = True
            self.changed = False
            self.dirty_files.pop(repo, None)
            if capability == "repo.start":
                self.started = True
            self.listed.pop(repo, None)
            self.touched.pop(repo, None)
        elif capability in ("repo.status", "repo.sync") and isinstance(out.get("clean"), bool):
            self.clean[repo] = out["clean"]
            changed = out.get("changed")
            if isinstance(changed, list):
                self.dirty_files[repo] = [str(c) for c in changed]
            if out["clean"]:
                self.changed = False
