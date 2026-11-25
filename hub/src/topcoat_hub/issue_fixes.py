"""The fix of an issue, from the run's edits to the hub that runs it (2026-11-17, D103, D104).

1. **Draft.** A run of ``issue_fix`` writes the fix as JSON: a summary, exact edits
   (``{path, old, new}``) and new files (a test). :func:`parse_fix` checks the form and the paths
   (only ``hub/``, ``proto/``, ``docs/``); the draft is a row of ``issue_fixes``, shown as a diff.
2. **Apply and test** (the person's one click on the whole set, D103). In the workshop of the dev
   node: a work branch ``topcoat/issue-<number>``, every edit, the diff, a commit, the tests started
   in the background on a copy of the worktree, and the workshop goes straight back to
   ``topcoat/live`` so other runs read the live code again. The hub follows the tests; a red test
   or an edit that did not fit starts the next ``issue_fix`` iteration with what went wrong as its
   feedback and the last version at the place that failed (at most :data:`AUTO_RETRIES` times in
   a row, and never after the same failure twice, failure_print.py; then an inbox item asks the
   person). A next draft that writes the same as the failed one where it failed is refused.
3. **Roll out** (a second click, admin, only a passed fix). The diff, mapped onto the hub's overlay
   (``hub/src/topcoat_hub`` → ``topcoat_hub``, ``hub/migrations`` → ``migrations``, ``proto``), goes
   into the spool directory of the hub host. A fixed script there (deploy/hub-fix-overlay.sh, run
   by systemd, not by the hub) checks the base, applies the patch to a copy of the overlay, dumps
   the database when migrations change, restarts the hub and goes back by itself when it does not
   answer (D104). The hub reads the outcome from the spool when it is up again.
4. **Confirm.** The person says whether it works: done, or a further iteration.

Nothing here runs without the person's click: the apply and the roll-out are refused for an API
key and for anyone but an admin, like every confirmed action (D15)."""

import asyncio
import contextlib
import difflib
import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from topcoat_hub import failure_print
from topcoat_hub.agent_workshop import CHANGES as WORKSHOP_EDITS
from topcoat_hub.models import IssueFix, ProjectNode
from topcoat_hub.tools import Caller, ToolError

log = logging.getLogger(__name__)

LIVE_REPO = "sloppy-topcoat-local"
LIVE_TOPIC = "live"
# web/src since 2026-11-20 (the maintainer: Topcoat fixes its own GUI too); the roll-out builds it
# on the hub host (deploy/fix-overlay/hub-fix-overlay.sh), package files stay out of reach
ALLOWED = ("hub/", "proto/", "docs/", "web/src/")
WEB_SRC = "web/src/"
# what of a diff reaches the hub's overlay, and where it lands there
OVERLAY_MAP = (
    ("hub/src/topcoat_hub/", "topcoat_hub/"),
    ("hub/migrations/", "migrations/"),
    ("proto/", "proto/"),
    # the GUI's source in the overlay; the host builds web-dist from it
    ("web/src/", "web-src/src/"),
)
# changed, but nothing the running hub reads: tests, docs, the rest of hub/ (pyproject, Dockerfile)
NOT_RUNTIME = ("hub/tests/", "docs/")
MAX_EDITS = 30
MAX_BYTES = 300_000
AUTO_RETRIES = 2
# the same failure (failure_print.py) this many times in a row: no further automatic draft
SAME_FAILURE_STOP = 2
KIND_BLOCKED = "fix_blocked"  # the inbox item when the automatic drafts stop
TEST_POLL_LIMIT = 60 * 60  # seconds the hub follows a test run
STATES_OPEN = ("draft", "applying", "testing", "passed", "queued")


class FixError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# --- pure ----------------------------------------------------------------------------------------


PLACES = "hub/, proto/, docs/ or web/src/"


def touches_web(paths: list[str]) -> bool:
    return any(p.startswith(WEB_SRC) for p in paths)


def _path_ok(path: str) -> bool:
    return (
        bool(path)
        and not path.startswith("/")
        and ".." not in path.split("/")
        and "\\" not in path
        and path.startswith(ALLOWED)
    )


def _json_object(text: str) -> dict[str, Any] | None:
    from topcoat_hub.ui.theme_patch import parse_patch  # the same tolerant reader

    return parse_patch(text)


def parse_fix(text: str) -> tuple[str, list[dict[str, str]], list[dict[str, str]], list[str]]:
    """``(summary, edits, files, problems)`` of a run's answer; problems make it no fix."""
    doc = _json_object(text or "")
    if doc is None:
        return "", [], [], ["the answer holds no JSON object"]
    problems: list[str] = []
    summary = " ".join(str(doc.get("summary") or "").split())[:500]
    edits: list[dict[str, str]] = []
    for i, e in enumerate(doc.get("edits") or [], start=1):
        if not isinstance(e, dict):
            problems.append(f"edit {i} is not an object")
            continue
        path, old, new = str(e.get("path") or "").strip(), e.get("old"), e.get("new")
        if not _path_ok(path):
            problems.append(f"edit {i}: {path!r} is not a file under {PLACES}")
        elif not isinstance(old, str) or not old.strip():
            problems.append(f"edit {i} ({path}): the old text is missing")
        elif not isinstance(new, str):
            problems.append(f"edit {i} ({path}): the new text is missing")
        elif old == new:
            problems.append(f"edit {i} ({path}): old and new are the same")
        else:
            edits.append({"path": path, "old": old, "new": new})
    files: list[dict[str, str]] = []
    for i, f in enumerate(doc.get("files") or [], start=1):
        if not isinstance(f, dict):
            problems.append(f"file {i} is not an object")
            continue
        path, content = str(f.get("path") or "").strip(), f.get("content")
        if not _path_ok(path):
            problems.append(f"file {i}: {path!r} is not a file under {PLACES}")
        elif not isinstance(content, str) or not content.strip():
            problems.append(f"file {i} ({path}): the content is missing")
        else:
            files.append({"path": path, "content": content})
    if not edits and not files:
        problems.append("the fix changes nothing")
    if len(edits) + len(files) > MAX_EDITS:
        problems.append(f"at most {MAX_EDITS} edits and files")
    size = sum(len(e["old"]) + len(e["new"]) for e in edits) + sum(len(f["content"]) for f in files)
    if size > MAX_BYTES:
        problems.append(f"the fix is larger than {MAX_BYTES} characters")
    return summary, edits, files, problems


def content_hash(edits: list[dict[str, str]], files: list[dict[str, str]]) -> str:
    raw = json.dumps({"edits": edits, "files": files}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def render(summary: str, edits: list[dict[str, str]], files: list[dict[str, str]]) -> str:
    """The draft as the person reads it: what it does, then every change as a diff."""
    parts = [f"**Fix-Entwurf:** {summary or '(ohne Zusammenfassung)'}"]
    for e in edits:
        diff = difflib.unified_diff(
            e["old"].splitlines(), e["new"].splitlines(), "vorher", "nachher", lineterm="", n=2
        )
        parts.append(f"### `{e['path']}`\n```diff\n" + "\n".join(diff) + "\n```")
    for f in files:
        lang = "python" if f["path"].endswith(".py") else "tsx"
        parts.append(f"### neu: `{f['path']}`\n```{lang}\n{f['content'].rstrip()}\n```")
    return "\n\n".join(parts)


def topic_of(number: str) -> str:
    return "issue-" + re.sub(r"[^0-9a-z]+", "-", number.lower()).strip("-")


def overlay_patch(diff: str) -> tuple[str, list[str], list[str]]:
    """The repository diff as a patch of the hub's overlay: ``(patch, left_out, refused)``.
    Tests and docs are left out (nothing the hub reads); web/src goes to the overlay's web-src (the
    host builds the GUI from it); a change anywhere else (node/, web/package.json,
    hub/pyproject.toml …) is refused: the overlay cannot carry it."""
    sections = re.split(r"(?m)^(?=diff --git )", diff)
    kept: list[str] = []
    left_out: list[str] = []
    refused: list[str] = []
    for section in sections:
        m = re.match(r"diff --git a/(\S+) b/(\S+)", section)
        if not m:
            continue
        path = m.group(2)
        target = next(
            (path.replace(src, dst, 1) for src, dst in OVERLAY_MAP if path.startswith(src)), None
        )
        if target is None:
            (left_out if path.startswith(NOT_RUNTIME) else refused).append(path)
            continue
        src = next(s for s, _ in OVERLAY_MAP if path.startswith(s))
        dst = dict(OVERLAY_MAP)[src]
        kept.append(section.replace(f"a/{src}", f"a/{dst}").replace(f"b/{src}", f"b/{dst}"))
    return "".join(kept), left_out, refused


def fix_view(f: IssueFix) -> dict[str, Any]:
    return {
        "id": str(f.id),
        "issue_id": str(f.issue_id),
        "summary": f.summary,
        "state": f.state,
        "edits": f.edits,
        "files": f.files,
        "branch": f.branch,
        "base_commit": f.base_commit,
        "commit": f.commit,
        "diff": f.diff,
        "tests": f.tests,
        "rollout": f.rollout,
        "error": f.error,
        # the same print on drafts in a row is the same failure (the GUI says "3 times the same")
        "failure_print": failure_print.of_fix(f) if f.state == "failed" else None,
        "created_at": f.created_at.isoformat() if f.created_at else None,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }


def _now() -> datetime:
    return datetime.now(UTC)


CONTEXT_LINES = 12  # lines shown on each side of the place a misquoted edit meant
# the line between the hub's tests and the web checks in the workshop image's output
# (deploy/workshop/pytest/run-tests.sh)
WEB_MARK = "=== web: lint, typecheck, tests"
_PASSED_LINE = re.compile(r"\b\d+ passed\b")
_RED = re.compile(r"\b\d+ (?:failed|errors?)\b")


def web_only_failure(log: str) -> str | None:
    """When the hub's tests of a workshop run passed and only its web checks failed: the first
    line of the web part, else ``None``. Live 2026-11-18: 3545 hub tests passed and the run was
    red on "Cannot find module 'mermaid'": the image's web packages are older than the repo's.
    A hub fix cannot change web/ (ALLOWED), so that failure is the image's, not the fix's. When
    the hub's summary is not in the kept output, nothing is assumed."""
    if WEB_MARK not in log:
        return None
    hub, web = log.split(WEB_MARK, 1)
    summary = [ln for ln in hub.splitlines() if _PASSED_LINE.search(ln)]
    if not summary or _RED.search(summary[-1]):
        return None
    return next((ln.strip() for ln in web.splitlines() if ln.strip()), "no output")[:300]


def verbatim(text: str, old: str) -> str | None:
    """The exact text of ``text`` that ``old`` quotes when it differs only in blank lines and in
    spaces at line ends, or ``None`` (no such place, or more than one). Live 2026-11-18: Qwen and
    gpt-oss wrote one blank line where the code has two, and every draft was refused for it.
    Whole lines only: the first and last quoted lines must be complete lines of the file."""
    want = [ln.rstrip() for ln in old.splitlines() if ln.strip()]
    if not want:
        return None
    lines = text.splitlines()
    found: list[tuple[int, int]] = []
    for start, line in enumerate(lines):
        if line.rstrip() != want[0]:
            continue
        k, i = 0, start
        while i < len(lines) and k < len(want):
            if not lines[i].strip():
                i += 1
                continue
            if lines[i].rstrip() != want[k]:
                break
            k, i = k + 1, i + 1
        if k == len(want):
            found.append((start, i))
    if len(found) != 1:
        return None
    start, end = found[0]
    exact = "\n".join(lines[start:end])
    return exact + "\n" if old.endswith("\n") else exact


def mended(old: str, new: str) -> str:
    """``new`` with each line restored that is a line of ``old`` with one to three of its first
    non-space characters lost, when that line of ``old`` is not in ``new`` otherwise. Live
    2026-11-21 (issue 2.10): a step that deleted a function kept the comment line below it as
    ``** The nodes …`` instead of ``/** The nodes …``, and the GUI did not parse. A line changed on
    purpose does not lose its first characters only."""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    kept = set(new_lines)
    out = []
    for line in new_lines:
        if line not in old_lines and line.strip():
            for orig in old_lines:
                body = orig.lstrip()
                indent = orig[: len(orig) - len(body)]
                lost = len(orig) - len(line)
                if (
                    orig not in kept
                    and 1 <= lost <= 3
                    and orig.endswith(line.lstrip())
                    and line.lstrip() == body[lost:]
                    and not body[:lost].isspace()
                    and line[: len(line) - len(line.lstrip())] in (indent, "")
                ):
                    line = orig
                    break
        out.append(line)
    return "\n".join(out) + ("\n" if new.endswith("\n") else "")


def shifted(text: str, old: str, new: str) -> tuple[str, str] | None:
    """An old text that is the file's lines word for word, but every line indented by the same
    wrong amount: ``(the file's lines, new moved by the same amount)``, or ``None``. Live
    2026-11-21 (issue 2.10): the writer quoted the toggle button of ListView with 16 spaces where
    the file has 14, in both tries, and the step was refused."""
    want = [ln for ln in old.splitlines() if ln.strip()]
    if not want:
        return None
    lines = text.splitlines()
    stripped = [ln.strip() for ln in lines]
    found: list[tuple[int, int, int]] = []
    for start, line in enumerate(stripped):
        if line != want[0].strip():
            continue
        k, i, deltas = 0, start, set()
        while i < len(lines) and k < len(want):
            if not stripped[i]:
                i += 1
                continue
            if stripped[i] != want[k].strip():
                break
            deltas.add(
                (len(lines[i]) - len(lines[i].lstrip())) - (len(want[k]) - len(want[k].lstrip()))
            )
            k, i = k + 1, i + 1
        if k == len(want) and len(deltas) == 1:
            found.append((start, i, deltas.pop()))
    if len(found) != 1:
        return None
    start, end, delta = found[0]
    exact = "\n".join(lines[start:end]) + ("\n" if old.endswith("\n") else "")
    moved = "\n".join(_reindent(new.splitlines(), delta)) + ("\n" if new.endswith("\n") else "")
    return exact, moved


def _reindent(lines: list[str], shift: int) -> list[str]:
    """``lines`` moved right by ``shift`` spaces (left when negative; blank lines stay blank)."""
    out = []
    for ln in lines:
        if not ln.strip():
            out.append("")
        elif shift >= 0:
            out.append(" " * shift + ln)
        else:
            cut = min(-shift, len(ln) - len(ln.lstrip()))
            out.append(ln[cut:])
    return out


def insertion(text: str, old: str, new: str) -> tuple[str, str] | None:
    """An edit that only adds lines right before or after one line of the file, written with
    context the file does not have: ``(old, new)`` that add the same lines at that line, or
    ``None``. Live 2026-11-21 (issue 2.10, the plan the person gave): the writer of "insert two
    functions before ``const select = (id: string) => {``" put a guessed ``setTab("tree");`` above
    the anchor, twice, and the step was refused although the lines to add were right. Only the
    added lines change the file; the guessed context is left as the file has it. The added lines
    take the indentation of the line they stand at."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    stripped_text = [ln.strip() for ln in text.splitlines()]
    file_lines = text.splitlines()
    o = [ln.strip() for ln in old_lines]
    n = [ln.strip() for ln in new_lines]
    candidates = [
        line
        for line in dict.fromkeys(o)
        if line and stripped_text.count(line) == 1 and o.count(line) == 1 and n.count(line) == 1
    ]

    def fits(line: str) -> tuple[int, int]:
        # the old text's lines that stand in the file at the same distance from it, then length
        # (a guessed context line is short; the line the step is anchored at is the distinct one)
        at, oi = stripped_text.index(line), o.index(line)
        same = sum(
            1
            for k, ln in enumerate(o)
            if 0 <= at + k - oi < len(stripped_text) and stripped_text[at + k - oi] == ln
        )
        return same, len(line)

    for line in sorted(candidates, key=fits, reverse=True):
        oi, ni = o.index(line), n.index(line)
        o_pre, o_post, n_pre, n_post = o[:oi], o[oi + 1 :], n[:ni], n[ni + 1 :]
        at = file_lines[stripped_text.index(line)]
        # the writer's indentation relative to the anchor line, moved to where the file has it
        shift = (len(at) - len(at.lstrip())) - (len(new_lines[ni]) - len(new_lines[ni].lstrip()))
        if o_post == n_post and n_pre[: len(o_pre)] == o_pre and len(n_pre) > len(o_pre):
            added = _reindent(new_lines[len(o_pre) : ni], shift)
            return at, "\n".join([*added, at])
        if (
            o_pre == n_pre
            and len(n_post) > len(o_post)
            and n_post[len(n_post) - len(o_post) :] == o_post
        ):
            added = _reindent(new_lines[ni + 1 : len(new_lines) - len(o_post)], shift)
            return at, "\n".join([at, *added])
    return None


def check_edits(
    edits: list[dict[str, str]], files: dict[str, str | None], *, first: int = 1
) -> tuple[list[dict[str, str]], list[str], dict[str, str | None]]:
    """The edits of a draft held against the files one after the other, as the workshop applies
    them: ``(edits, problems, files after the edits)``. ``files`` maps a path to its text, ``None``
    when it does not exist; a path missing from it was not read and is not checked.

    An old text must occur exactly once in the file *as the edits before it left it*, so a second
    edit may change what the first one wrote (2026-11-19: fixes in steps build on each other; held
    against the live file, every such edit was refused). An old text that misses the code only by
    blank lines or line-end spaces takes the code's own words (:func:`verbatim`). Live 2026-11-18:
    drafts quoted code the run had not read (an invented body of ``_children``) or a placeholder
    ("# ... other code ..."); that showed only on the click, after a whole cycle. Where the first
    line of a missing old text stands in the file, the real lines around it are given, so the next
    draft can copy them verbatim. ``first`` is the number of the first edit in the problems."""
    state = dict(files)
    out: list[dict[str, str]] = []
    problems: list[str] = []
    for i, edit in enumerate(edits, first):
        path, old = edit.get("path", ""), edit.get("old", "")
        if old and edit.get("new") and (fixed := mended(old, edit["new"])) != edit["new"]:
            edit = {**edit, "new": fixed}  # a kept line that lost its first characters
        text = state.get(path)
        if text is not None and text.count(old) == 0 and (exact := verbatim(text, old)):
            edit = {**edit, "old": exact}
            old = exact
        elif (
            text is not None
            and old
            and text.count(old) == 0
            and (
                fitted := shifted(text, old, edit.get("new", ""))
                or insertion(text, old, edit.get("new", ""))
            )
            and text.count(fitted[0]) == 1
        ):
            edit = {**edit, "old": fitted[0], "new": fitted[1]}
            old = fitted[0]
        out.append(edit)
        if text is None:
            if path in state:
                problems.append(f"edit {i}: {path} does not exist in the live code")
            continue  # else not read (a new file, or the workshop was not reachable)
        n = text.count(old)
        if n == 1:
            state[path] = text.replace(old, edit.get("new", ""), 1)
            continue
        if n > 1:
            problems.append(
                f"edit {i} ({path}): the old text occurs {n} times; add lines of context so it "
                "occurs exactly once"
            )
            continue
        problem = f"edit {i} ({path}): the old text is not in the file (copy it verbatim)"
        head = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")
        lines = text.splitlines()
        at = next((k for k, ln in enumerate(lines) if head and head in ln), None)
        if at is None and head:  # the first line misquoted too ("parse(txt)"): the closest one
            stripped = [ln.strip() for ln in lines]
            close = difflib.get_close_matches(head, stripped, n=1, cutoff=0.75)
            at = stripped.index(close[0]) if close else None
        if at is not None:
            lo, hi = max(at - CONTEXT_LINES, 0), min(at + CONTEXT_LINES + 1, len(lines))
            window = "\n".join(lines[lo:hi])
            problem += f"; the file has there (lines {lo + 1}-{hi}):\n```\n{window}\n```"
        problems.append(problem)
    return out, problems, state


def fit_edits(edits: list[dict[str, str]], files: dict[str, str | None]) -> list[dict[str, str]]:
    """The edits of :func:`check_edits`, an old text fitted to the code's own words."""
    return check_edits(edits, files)[0]


def edit_problems(edits: list[dict[str, str]], files: dict[str, str | None]) -> list[str]:
    """The problems of :func:`check_edits`."""
    return check_edits(edits, files)[1]


# --- the service ---------------------------------------------------------------------------------


class FixService:
    """Drafts, the workshop steps, the tests, the roll-out. One apply at a time (the workshop has
    one scratch copy per repository)."""

    def __init__(self, sessionmaker: Any, tools: Any, settings: Any) -> None:
        self.sessionmaker = sessionmaker
        self.tools = tools
        self.settings = settings
        self.runs: Any = None  # RunService, for the next iteration after a red test (main.py)
        self.bus: Any = None  # EventBus, to tell the inbox (main.py)
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Future[None]] = set()

    # -- rows --

    async def create(
        self,
        user_id: uuid.UUID,
        issue_id: uuid.UUID,
        run_id: uuid.UUID | None,
        summary: str,
        edits: list[dict[str, str]],
        files: list[dict[str, str]],
    ) -> IssueFix:
        row = IssueFix(
            id=uuid.uuid4(),
            user_id=user_id,
            issue_id=issue_id,
            run_id=run_id,
            summary=summary,
            edits=edits,
            files=files,
            content_hash=content_hash(edits, files),
            state="draft",
        )
        async with self.sessionmaker() as db:
            # an older draft of the same issue is no longer the one to apply
            for old in await db.scalars(
                select(IssueFix).where(
                    IssueFix.user_id == user_id,
                    IssueFix.issue_id == issue_id,
                    IssueFix.state == "draft",
                )
            ):
                old.state = "discarded"
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row

    async def of_issue(self, user_id: uuid.UUID, issue_id: uuid.UUID) -> list[IssueFix]:
        async with self.sessionmaker() as db:
            return list(
                await db.scalars(
                    select(IssueFix)
                    .where(IssueFix.user_id == user_id, IssueFix.issue_id == issue_id)
                    .order_by(IssueFix.created_at)
                )
            )

    async def get(self, user_id: uuid.UUID, fix_id: uuid.UUID) -> IssueFix:
        async with self.sessionmaker() as db:
            row = await db.get(IssueFix, fix_id)
        if row is None or row.user_id != user_id:
            raise FixError(404, "not_found", "no such fix")
        return row

    async def live_fixes(self, user_id: uuid.UUID) -> list[IssueFix]:
        async with self.sessionmaker() as db:
            return list(
                await db.scalars(
                    select(IssueFix)
                    .where(IssueFix.user_id == user_id, IssueFix.state == "live")
                    .order_by(IssueFix.updated_at)
                )
            )

    async def _set(self, fix_id: uuid.UUID, **fields: Any) -> IssueFix:
        async with self.sessionmaker() as db:
            row = await db.get(IssueFix, fix_id)
            if row is None:
                raise FixError(404, "not_found", "no such fix")
            for k, v in fields.items():
                setattr(row, k, v)
            row.updated_at = _now()
            await db.commit()
            await db.refresh(row)
            return row

    async def discard(self, user_id: uuid.UUID, fix_id: uuid.UUID) -> IssueFix:
        row = await self.get(user_id, fix_id)
        if row.state not in ("draft", "failed", "passed", "refused", "rolled_back"):
            raise FixError(409, "busy", f"a fix that is {row.state} is not discarded")
        return await self._set(fix_id, state="discarded")

    # -- apply and test (D103) --

    async def _live_text(self, caller: Caller, node: uuid.UUID, path: str) -> str | None:
        """The whole file of the live code (repo.read gives 400 lines a call), ``None`` when the
        workshop has no such file."""
        parts: list[str] = []
        start = 1
        for _ in range(100):  # 40,000 lines at most
            try:
                out = await self._read(
                    caller,
                    node,
                    "repo.read",
                    {"repo": LIVE_REPO, "path": path, "start_line": start},
                )
            except FixError:
                return None if start == 1 else "\n".join(parts)
            parts.append(str(out.get("text") or ""))
            if not out.get("more_after"):
                break
            start = int(out.get("end_line") or start) + 1
        return "\n".join(parts) + "\n"

    async def verify(
        self,
        user_id: uuid.UUID,
        edits: list[dict[str, str]],
        new_files: list[dict[str, str]] | None = None,
    ) -> tuple[list[dict[str, str]], list[str]]:
        """The problems of a draft against the live code (:func:`edit_problems`), and a "new" file
        that already exists (live 2026-11-18: a draft wrote hub/tests/test_project_runs.py anew,
        which would have dropped every test in it). Returns the edits, an old text that misses the
        code only by blank lines taken verbatim from it (:func:`fit_edits`), and the problems:
        none when it fits, or when the workshop cannot be read (the click finds out then).
        A new *test* file that exists is renamed to a free name in ``new_files`` itself (1.15)."""
        paths = {e.get("path", "") for e in edits}
        news = [f.get("path", "") for f in new_files or []]
        files = await self.read_live(user_id, sorted(paths | set(news)))
        edits, problems, _ = check_edits(edits, {p: t for p, t in files.items() if p in paths})
        from topcoat_hub.fix_steps import _is_test_file, free_test_path

        async def read(more: list[str]) -> dict[str, str | None]:
            return await self.read_live(user_id, more)

        for f in new_files or []:
            path = f.get("path", "")
            if files.get(path) is not None and _is_test_file(path):
                # a test file that exists gets a free name, no refusal (1.15, live 2026-11-23:
                # three drafts were refused for it); the draft's file list is renamed in place
                free, note = await free_test_path(path, read)
                if note:
                    f["path"] = free
                    files[path] = None
        for path in news:
            if files.get(path) is not None:
                problems.append(
                    f"file {path} exists already: a file of the draft must be new; change an "
                    "existing file with edits, or give the test a new file name"
                )
        return edits, problems

    async def read_live(self, user_id: uuid.UUID, paths: list[str]) -> dict[str, str | None]:
        """The live texts of ``paths`` (``None``: no such file); a path that could not be read, or
        all of them when no workshop is there, is left out."""
        caller = Caller(user_id, True, "agent", application="dev")
        try:
            node = await self._dev_node(caller)
        except Exception:
            return {}
        files: dict[str, str | None] = {}
        for path in paths:
            try:
                files[path] = await self._live_text(caller, node, path)
            except Exception:
                log.info("fix check: %s could not be read", path, exc_info=True)
        return files

    async def outline_live(self, user_id: uuid.UUID, path: str) -> list[dict[str, Any]]:
        """The workshop's map of ``path`` in the live code (``repo.outline``, plan step 1.11): its
        files with their named places and lines; [] when no workshop or a node without the
        capability is there (it needs a node release, D123)."""
        caller = Caller(user_id, True, "agent", application="dev")
        try:
            node = await self._dev_node(caller)
            out = await self._read(caller, node, "repo.outline", {"repo": LIVE_REPO, "path": path})
        except Exception:
            log.info("fix check: no outline of %s", path, exc_info=True)
            return []
        files = out.get("files") if isinstance(out, dict) else None
        return [f for f in files or [] if isinstance(f, dict)]

    async def program_of(self, fix: IssueFix) -> str:
        """The program whose run wrote the draft (``issue_fix`` when that is not known)."""
        from topcoat_hub.models import AgentTask

        if fix.run_id is None:
            return "issue_fix"
        async with self.sessionmaker() as db:
            task = await db.get(AgentTask, fix.run_id)
        name = str(((task.diagnosis or {}) if task else {}).get("program") or "")
        return name if name in ("issue_fix", "issue_steps", "issue_plan") else "issue_fix"

    async def last_program(self, user_id, issue_id) -> str:
        """The program of the latest fix for the issue, or 'issue_fix' if no fix exists."""
        fixes = await self.of_issue(user_id, issue_id)
        if not fixes:
            return "issue_fix"
        latest_fix = fixes[-1]
        return await self.program_of(latest_fix)

    async def refuse_draft(self, fix_id: uuid.UUID, why: str) -> None:
        """A draft whose edits do not fit the code: failed at once, before any click."""
        await self._set(fix_id, state="failed", error=why[:2000])

    async def _dev_node(self, caller: Caller) -> uuid.UUID:
        offers = [
            t
            for t in await self.tools.proposable_tools(caller)
            if t.get("capability") == "repo.edit"
        ]
        if not offers:
            raise FixError(409, "no_workshop", "no connected dev node offers the workshop")
        return uuid.UUID(str(offers[0]["node_id"]))

    async def _read(self, caller: Caller, node: uuid.UUID, cap: str, data: dict[str, Any]) -> Any:
        res = await self.tools.invoke(caller, node, cap, data)
        if not res.get("ok", True):
            err = res.get("error") or {}
            raise FixError(
                502, "workshop", f"{cap}: {err.get('message') if isinstance(err, dict) else err}"
            )
        return res.get("output", res)

    async def _run(
        self, caller: Caller, node: uuid.UUID, fix: IssueFix, cap: str, data: dict[str, Any]
    ) -> Any:
        if cap in WORKSHOP_EDITS:  # the edits are the confirmed set itself (D103), audited as such
            res = await self.tools.execute_confirmed(
                caller, node, cap, data, fix.id, fix.content_hash
            )
        else:  # a scratch step of the workshop: an admin on the GUI runs it without a click (D52)
            res = await self.tools.invoke(caller, node, cap, data)
        if not res.get("ok"):
            err = res.get("error") or {}
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise FixError(502, "workshop", f"{cap}: {message or 'failed'}")
        return res.get("output")

    async def apply(self, caller: Caller, fix_id: uuid.UUID) -> IssueFix:
        """The one click on the whole fix (D103). Refused for an API key and for anyone but an
        admin; the steps run in the background, the row shows where they are."""
        if caller.via != "gui" or not caller.admin:
            raise FixError(403, "gui_admin", "only the signed-in admin applies a fix")
        fix = await self.get(caller.user_id, fix_id)
        if fix.state != "draft":
            raise FixError(409, "not_draft", f"this fix is {fix.state}")
        if content_hash(fix.edits, fix.files) != fix.content_hash:
            await self._set(fix.id, state="refused", error="the fix does not match its hash")
            raise FixError(409, "content_changed", "the fix does not match its hash: not applied")
        if self._lock.locked():
            raise FixError(409, "busy", "another fix is being applied; try again in a minute")
        fix = await self._set(fix.id, state="applying", decided_by=caller.user_id, error=None)
        task = asyncio.ensure_future(self._apply(caller, fix))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return fix

    async def _apply(self, caller: Caller, fix: IssueFix) -> None:
        async with self._lock:
            try:
                await self._apply_steps(caller, fix)
            except FixError as exc:
                await self._failed(caller, fix.id, exc.message, retry=True)
            except ToolError as exc:
                await self._failed(caller, fix.id, f"{exc.code}: {exc.message}", retry=True)
            except Exception as exc:  # the outcome is unknown: say so, never "nothing happened"
                log.exception("fix %s: applying failed", fix.id)
                await self._failed(caller, fix.id, f"unexpected: {exc}", retry=False)

    async def _apply_steps(self, caller: Caller, fix: IssueFix) -> None:
        async with self.sessionmaker() as db:
            issue = await db.get(ProjectNode, fix.issue_id)
        if issue is None:
            raise FixError(404, "not_found", "the issue is gone")
        node = await self._dev_node(caller)
        repo = {"repo": LIVE_REPO}
        status = await self._read(caller, node, "repo.status", repo)
        if not status.get("clean", False):
            raise FixError(
                409, "dirty", "the workshop copy has unsaved changes; nothing was applied"
            )
        base = str(status.get("head") or "")
        # one branch per draft: a second draft of the same issue starts from the live code again,
        # and the branch of the first one is not its history (live 2026-11-18: "rejected
        # (non-fast-forward)" on topcoat/issue-2-2)
        topic = f"{topic_of(issue.number)}-{fix.id.hex[:6]}"
        branch = f"topcoat/{topic}"
        await self._set(fix.id, base_commit=base, branch=branch)
        await self._run(caller, node, fix, "repo.start", {**repo, "topic": topic})
        try:
            for e in fix.edits:
                await self._run(caller, node, fix, "repo.edit", {**repo, **e})
            for f in fix.files:
                await self._run(caller, node, fix, "repo.write", {**repo, **f})
            diff = await self._read(caller, node, "repo.diff", repo)
            text = str(diff.get("diff") or "")
            notes = []
            if diff.get("truncated"):
                notes.append("the diff was cut")
            if diff.get("masked"):
                notes.append("the diff had masked values")
            message = f"Fix {issue.number}: {fix.summary or issue.title}"[:300]
            await self._run(caller, node, fix, "repo.commit", {**repo, "message": message})
            head = await self._read(caller, node, "repo.status", repo)
            started = await self._run(caller, node, fix, "repo.test_start", repo)
            job = str((started or {}).get("job_id") or "")
        except BaseException:
            # back to the live code, the fix's branch keeps what was done
            with contextlib.suppress(Exception):
                await self._run(
                    caller, node, fix, "repo.start", {**repo, "topic": LIVE_TOPIC, "discard": True}
                )
            raise
        # the tests run on a copy: the workshop goes back to the live code at once
        await self._run(caller, node, fix, "repo.start", {**repo, "topic": LIVE_TOPIC})
        await self._set(
            fix.id,
            state="testing",
            diff=text,
            commit=str(head.get("head") or ""),
            tests={
                "job_id": job,
                "state": "running",
                "started_at": _now().isoformat(),
                "notes": notes,
            },
        )
        await self._follow(caller, fix.id, node, job)

    async def _follow(self, caller: Caller, fix_id: uuid.UUID, node: uuid.UUID, job: str) -> None:
        waited = 0
        out: dict[str, Any] = {}
        while waited < TEST_POLL_LIMIT:
            out = await self._read(
                caller,
                node,
                "repo.test_status",
                {"repo": LIVE_REPO, "job_id": job, "wait_seconds": 15},
            )
            if not out.get("running"):
                break
            waited += 15
        fix = await self.get(caller.user_id, fix_id)
        state = str(out.get("state") or "unknown")
        tests = {
            **(fix.tests or {}),
            "state": state,
            "output": str(out.get("log") or "")[-6000:],
            "ended_at": _now().isoformat(),
        }
        if state == "passed":
            await self._set(fix_id, state="passed", tests=tests)
            return
        web = web_only_failure(tests["output"])
        paths = [str(e.get("path") or "") for e in [*(fix.edits or []), *(fix.files or [])]]
        if web is not None and not touches_web(paths):
            # a fix of the hub alone: a red web part is the image's (older packages), not the fix's
            tests["web"] = f"the web checks of the workshop failed, not part of this fix: {web}"
            await self._set(fix_id, state="passed", tests=tests)
            return
        await self._set(fix_id, tests=tests)
        await self._failed(caller, fix_id, f"the tests ended {state}", retry=True)

    async def _failed(self, caller: Caller, fix_id: uuid.UUID, why: str, *, retry: bool) -> None:
        fix = await self._set(fix_id, state="failed", error=why[:2000])
        if retry:
            await self.next_draft(fix, why)

    async def next_draft(self, fix: IssueFix, why: str) -> None:
        """The next draft of a failed fix, with what went wrong and the last version at the place
        that failed. None when the same failure came twice in a row (failure_print.py) or the
        issue's drafts already failed AUTO_RETRIES times in a row: then the person decides, told
        by an inbox item."""
        if self.runs is None:
            return
        failed: list[IssueFix] = []
        for f in reversed(await self.of_issue(fix.user_id, fix.issue_id)):
            if f.state == "failed":
                failed.append(f)
            elif f.state not in ("discarded",):
                break
        prints = [failure_print.of_fix(f) for f in failed]
        same = failure_print.repeats(prints)
        if same >= SAME_FAILURE_STOP:
            await self._blocked(fix, f"the same failure {same} times in a row: {prints[0]}")
            return
        if len(failed) > AUTO_RETRIES:
            await self._blocked(fix, f"{len(failed)} drafts failed in a row")
            return
        output = str((fix.tests or {}).get("output") or "")[-2500:]
        feedback = f"Der Fix scheiterte: {why}." + (f"\n\nTestausgabe:\n{output}" if output else "")
        # a fix in steps is planned again in steps, from the live code, with the draft that failed
        # beside it (the failed branch is not what the hub runs)
        program = await self.program_of(fix)
        # a red fix of a settled plan is repaired in steps too, on top of it
        program = "issue_steps" if program == "issue_plan" else program
        spot = failure_print.spot_paths(
            [x.get("path", "") for x in [*(fix.edits or []), *(fix.files or [])]], why, output
        )
        if program == "issue_steps" and fix.edits:
            feedback += (
                "\n\nDer gescheiterte Entwurf, auf dem die Reparatur aufbaut (seine Edits "
                "bleiben; plane nur die Schritte, die ihn reparieren, keine, die ihn neu "
                "schreiben; ist nur der Test falsch, genügt der Test):\n"
                + render(fix.summary, fix.edits, fix.files)[:5000]
            )
        elif spot:
            # the last version at the place that failed: the same text there fails the same way
            keep = set(spot)
            feedback += (
                "\n\nDie letzte Fassung an der Fehlerstelle:\n"
                + render(
                    fix.summary,
                    [e for e in fix.edits or [] if e.get("path") in keep],
                    [f for f in fix.files or [] if f.get("path") in keep],
                )[:4000]
            )
        if spot:
            feedback += (
                f"\n\nEin neuer Entwurf muss an der Fehlerstelle ({', '.join(spot)}) etwas "
                "ändern; bleibt er dort gleich, wird er abgelehnt."
            )
        try:
            from topcoat_hub.project_events import PERSON
            from topcoat_hub.project_iterations import IterationStore

            await IterationStore(self.sessionmaker, PERSON).begin(
                fix.user_id, fix.issue_id, based_on=None, feedback=feedback, keep=False
            )
            await self.runs.start_node(fix.user_id, fix.issue_id, iterate=True, program=program)
        except Exception as exc:  # the person can still start it by hand
            log.info("fix %s: the next iteration did not start (%s)", fix.id, exc)

    async def unchanged_at_failure(
        self,
        user_id: uuid.UUID,
        issue_id: uuid.UUID,
        edits: list[dict[str, str]],
        files: list[dict[str, str]],
    ) -> str | None:
        """Why a new draft is refused at once: it writes the same as the issue's last failed draft
        into the files the failure named (None when it changes something there, or when the last
        draft did not fail). Call it before :meth:`create` (which discards older drafts)."""
        last = None
        for f in reversed(await self.of_issue(user_id, issue_id)):
            if f.state != "discarded":
                last = f
                break
        if last is None or last.state != "failed":
            return None
        before_edits, before_files = list(last.edits or []), list(last.files or [])
        spot = failure_print.spot_paths(
            [x.get("path", "") for x in [*before_edits, *before_files]],
            str(last.error or ""),
            str((last.tests or {}).get("output") or ""),
        )
        before = failure_print.at_spot(before_edits, before_files, spot)
        if before and failure_print.at_spot(edits, files, spot) == before:
            return (
                "the draft is the same as the failed one at the place that failed "
                f"({', '.join(spot)}): it would fail the same way"
            )
        return None

    async def _blocked(self, fix: IssueFix, why: str) -> None:
        """The automatic drafts stop: an inbox item says why and opens the issue."""
        from topcoat_hub.inbox import Candidate, InboxStore
        from topcoat_hub.project_base import APPLICATION

        log.info("fix %s: no further draft (%s)", fix.id, why)
        async with self.sessionmaker() as db:
            node = await db.get(ProjectNode, fix.issue_id)
            if node is None or node.user_id != fix.user_id:
                return
            from topcoat_hub.vorhaben import large_of

            if large_of(node.body or "") is not None:
                # a sub-task of a large task: the coordinator replans its branch first and asks
                # the person only when that fails (D109, vorhaben.Coordinator.tick)
                return
            await InboxStore(db, fix.user_id).add(
                Candidate(
                    kind=KIND_BLOCKED,
                    source_type="issue_fix",
                    source_ref=str(fix.id),
                    title=f"Fix blocked: {node.number} {node.title}",
                    summary=why,
                    application=APPLICATION,
                    ref={
                        "module": "page.project",
                        "props": {"project": str(node.project_id), "node": str(node.id)},
                        "project_id": str(node.project_id),
                        "node_id": str(node.id),
                    },
                )
            )
            await db.commit()
        if self.bus is not None:
            self.bus.publish("inbox", {"_user": str(fix.user_id)})

    async def resume(self) -> None:
        """After a restart: fixes whose tests were being followed are followed again."""
        async with self.sessionmaker() as db:
            rows = list(await db.scalars(select(IssueFix).where(IssueFix.state == "testing")))
        for fix in rows:
            job = str((fix.tests or {}).get("job_id") or "")
            if not job or fix.decided_by is None:
                continue
            caller = Caller(fix.decided_by, True, "gui")
            try:
                node = await self._dev_node(caller)
            except FixError:
                continue
            task = asyncio.ensure_future(self._follow(caller, fix.id, node, job))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # -- roll-out (D104) --

    def _spool(self) -> Path | None:
        raw = str(getattr(self.settings, "fix_spool_dir", "") or "").strip()
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_dir() else None

    async def rollout(self, caller: Caller, fix_id: uuid.UUID) -> IssueFix:
        """The second click: a passed fix goes to the host's roll-out (D104)."""
        if caller.via != "gui" or not caller.admin:
            raise FixError(403, "gui_admin", "only the signed-in admin rolls a fix out")
        fix = await self.get(caller.user_id, fix_id)
        if fix.state != "passed":
            raise FixError(
                409, "not_passed", f"only a fix whose tests passed is rolled out ({fix.state})"
            )
        notes = (fix.tests or {}).get("notes") or []
        if notes:
            raise FixError(
                409, "diff_incomplete", f"the recorded diff is not whole ({'; '.join(notes)})"
            )
        patch, left_out, refused = overlay_patch(fix.diff)
        if refused:
            raise FixError(
                409,
                "not_overlay",
                "the fix changes files the hub's overlay cannot carry: " + ", ".join(refused[:6]),
            )
        if not patch.strip():
            await self._set(
                fix.id,
                state="live",
                rollout={
                    "state": "nothing",
                    "message": "only tests or docs changed",
                    "at": _now().isoformat(),
                },
            )
            return await self.get(caller.user_id, fix_id)
        spool = self._spool()
        if spool is None:
            raise FixError(409, "no_spool", "this hub has no roll-out spool (fix_spool_dir)")
        async with self.sessionmaker() as db:
            issue = await db.get(ProjectNode, fix.issue_id)
        number = issue.number if issue is not None else "?"
        request = spool / "request" / str(fix.id)
        manifest = {
            "id": str(fix.id),
            "label": f"fix-{number}",
            "base_commit": (fix.base_commit or "")[:12],
            "migrations": "migrations/" in patch,
            # the host builds the GUI anew from web-src (typecheck, tests, build) before the swap
            "web": "web-src/" in patch,
            "left_out": left_out,
        }

        def write() -> None:
            request.mkdir(parents=True, exist_ok=True)
            (request / "fix.patch").write_text(patch, encoding="utf-8")
            (request / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (request / "READY").write_text("", encoding="utf-8")  # the host acts on this file

        await asyncio.to_thread(write)
        return await self._set(
            fix.id,
            state="queued",
            rollout={"state": "queued", "label": manifest["label"], "at": _now().isoformat()},
        )

    async def sync_rollouts(self) -> int:
        """Read the host's outcomes (spool/done/<id>.json) into the fixes waiting for one."""
        spool = self._spool()
        if spool is None:
            return 0
        async with self.sessionmaker() as db:
            waiting = list(await db.scalars(select(IssueFix).where(IssueFix.state == "queued")))
        done = 0
        for fix in waiting:
            path = spool / "done" / f"{fix.id}.json"
            try:
                result = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            outcome = str(result.get("state") or "")
            state = {"live": "live", "rolled_back": "rolled_back"}.get(outcome, "refused")
            await self._set(
                fix.id,
                state=state,
                rollout={
                    **(fix.rollout or {}),
                    **{k: result.get(k) for k in ("state", "build", "message", "output", "at")},
                },
            )
            done += 1
        return done
