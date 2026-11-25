"""Registered repositories (E6-01 S0, D52, D76).

* **External** — it lives elsewhere (GitHub): a person adds it once (URL, token, branch, test
  template) and the assistant's GitHub tool reads it. Only reading happens here: the token is used
  for ``GET`` through :class:`GitHubReader`, never to write anything.
* **Local** (D76) — its origin lives in the workshop of a dev node, where the assistant edits and
  tests it on work branches. It is created there, or made as a local clone of an external one (its
  upstream, where it is published to). The person's own steps (create, clone, branch, take a
  branch into the default branch, publish, pull, remove) run on the node through the action
  ``workshop.manage`` (:func:`topcoat_hub.tools.manage`), only from the hub's page.

Rows are per person (D8) and every query is scoped by ``user_id``. The token is stored encrypted
(:mod:`topcoat_hub.secretbox`) and is never returned.
"""

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.config import Settings
from topcoat_hub.github_access import GitHubError, GitHubReader
from topcoat_hub.models import DevRepo, Node
from topcoat_hub.secretbox import SecretBox, SecretError
from topcoat_hub.tools import ToolError

MODES = ("read", "test-branch")
KINDS = ("external", "local")
# the workshop capabilities that change the scratch worktree or run its tests: only a repository in
# mode test-branch gets them (a read-only repository can be cloned, read and searched, nothing more)
WORKTREE_CAPABILITIES = frozenset(
    {
        "repo.start",
        "repo.edit",
        "repo.write",
        "repo.apply_patch",
        "repo.reset",
        "repo.test",
        "repo.test_start",
        "repo.test_cancel",
        "repo.build_node",
        "repo.push",
    }
)
# the test commands a repository may use: a name, never free text (D52)
TEMPLATES = ("none", "make-check", "npm-test", "go-test", "pytest")
TEST_BRANCH_PREFIX = "topcoat/"

# the name is also the directory of the clone on the workshop node: a short lower-case slug
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_URL = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)?"
    r"(?P<owner>[A-Za-z0-9_.-]{1,100})/(?P<repo>[A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?$"
)


class RepoError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def parse_repo(text: str) -> str:
    """``owner/name`` from ``owner/name``, an https GitHub URL or an ssh one."""
    m = _URL.match(text.strip())
    if m is None:
        raise RepoError(422, "invalid_repo", "use owner/name or a github.com address")
    return f"{m['owner']}/{m['repo']}"


def check_fields(name: str, mode: str, template: str) -> None:
    if not _NAME.match(name):
        raise RepoError(
            422, "invalid_name", "the name must be a short slug: lower-case letters, digits, . _ -"
        )
    if mode not in MODES:
        raise RepoError(422, "invalid_mode", f"mode must be one of: {', '.join(MODES)}")
    if template not in TEMPLATES:
        raise RepoError(422, "invalid_template", f"template must be one of: {', '.join(TEMPLATES)}")


async def read_state(reader: GitHubReader) -> dict[str, Any]:
    """What the repository looks like now, from three ``GET``s."""
    info = await reader.get(f"/repos/{reader.repo}")
    if not isinstance(info, dict):
        raise GitHubError(502, "bad_answer", "GitHub answered with an unexpected shape")
    branches = await reader.branches(1)
    try:
        prs: Any = await reader.prs("open", 1)
    except GitHubError as exc:
        # reading pull requests is optional (a token without that right still clones and pushes)
        if exc.code != "forbidden_or_limit":
            raise
        prs = None
    names = [str(b.get("name", "")) for b in branches if isinstance(b, dict)]
    return {
        "default_branch": str(info.get("default_branch") or "main"),
        "private": bool(info.get("private")),
        "pushed_at": info.get("pushed_at"),
        "open_prs": len(prs) if isinstance(prs, list) else None,
        "branches": len(names),
        "test_branches": [n for n in names if n.startswith(TEST_BRANCH_PREFIX)][:20],
        "can_push": bool((info.get("permissions") or {}).get("push")),
    }


def view(row: DevRepo) -> dict[str, Any]:
    """The row as the GUI sees it; the token is never part of it."""
    return {
        "id": str(row.id),
        "kind": row.kind,
        "node_id": str(row.node_id) if row.node_id else None,
        "upstream_id": str(row.upstream_id) if row.upstream_id else None,
        "name": row.name,
        "full_name": row.full_name,
        "default_branch": row.default_branch,
        "mode": row.mode,
        "test_template": row.test_template,
        "token_set": bool(row.token_enc),
        "state": row.last_state,
        "checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


class DevRepoStore:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], box: SecretBox, settings: Settings
    ) -> None:
        self.sessionmaker = sessionmaker
        self.box = box
        self.settings = settings
        # the tool service (set at start): the person's steps on local repositories run through it
        self.tools: Any = None

    def reader(self, row: DevRepo) -> GitHubReader:
        try:
            token = self.box.decrypt(row.token_enc)
        except SecretError as exc:
            raise RepoError(409, "token_unreadable", str(exc)) from exc
        return GitHubReader.for_repo(self.settings, token, row.full_name)

    async def call_spec(self, user_id: uuid.UUID, capability: str, ref: Any) -> dict[str, str]:
        """What a dev node needs to work on one repository for one call: its address, default
        branch, test template and token. The node gets it with the call only and never keeps it.
        ``ref`` is the repository as the caller named it (name, id or ``owner/name``)."""
        if not isinstance(ref, str) or not ref:
            raise ToolError(422, "repo_needed", "name the repository in `repo`")
        row = await self.find(user_id, ref)
        if row is None:
            names = ", ".join(r.name for r in await self.list(user_id)) or "none registered"
            raise ToolError(422, "unknown_repo", f"no such repository; registered: {names}")
        if row.kind != "local":
            # D76: once an external repository has a local clone, the clone is where its code
            # is read and changed (the node's own copy of the remote may be old)
            clone = next(
                (
                    r
                    for r in await self.list(user_id)
                    if r.kind == "local" and r.upstream_id == row.id
                ),
                None,
            )
            if clone is not None:
                row = clone
        if row.kind == "local":
            # D76: the origin is in the workshop of its dev node; the node clones from there
            return {
                "name": row.name,
                "url": "",
                "branch": row.default_branch,
                "template": row.test_template,
                "token": "",
                "local": True,  # type: ignore[dict-item]
                "node_id": str(row.node_id) if row.node_id else "",
            }
        if capability in ("repo.commit", "repo.branches"):
            raise ToolError(
                403,
                "repo_external",
                f"{row.name} lives elsewhere: make a local clone of it to keep work there",
            )
        if capability in WORKTREE_CAPABILITIES and row.mode != "test-branch":
            raise ToolError(
                403,
                "repo_read_only",
                f"{row.name} lives elsewhere and is only read: make a local clone of it to "
                "edit and test it",
            )
        try:
            token = self.box.decrypt(row.token_enc)
        except SecretError as exc:
            raise ToolError(409, "token_unreadable", str(exc)) from exc
        return {
            "name": row.name,  # the node's directory for it
            "url": f"https://github.com/{row.full_name}.git",
            "branch": row.default_branch,
            "template": row.test_template,
            "token": token,
        }

    async def list(self, user_id: uuid.UUID) -> list[DevRepo]:
        async with self.sessionmaker() as db:
            rows = await db.scalars(
                select(DevRepo).where(DevRepo.user_id == user_id).order_by(DevRepo.name)
            )
            return list(rows)

    async def find(self, user_id: uuid.UUID, ref: str) -> DevRepo | None:
        """By id, name or ``owner/name`` (case does not matter, a GitHub URL or a bare repository
        name is enough when only one row fits); always one of this person's rows."""
        rows = await self.list(user_id)
        for row in rows:
            if ref in (str(row.id), row.name, row.full_name):
                return row
        wanted = ref.strip().casefold().removesuffix(".git").rstrip("/")
        wanted = wanted.removeprefix("https://github.com/").removeprefix("github.com/")
        fits = [r for r in rows if wanted in (r.name.casefold(), r.full_name.casefold())]
        if not fits:  # the repository part alone: "sloppy-topcoat" for acme/sloppy-topcoat
            fits = [r for r in rows if r.full_name.casefold().rsplit("/", 1)[-1] == wanted]
        return fits[0] if len(fits) == 1 else None

    async def add(
        self,
        user_id: uuid.UUID,
        *,
        name: str,
        repo: str,
        token: str,
        mode: str,
        template: str,
    ) -> DevRepo:
        check_fields(name, mode, template)
        full = parse_repo(repo)
        if not token.strip():
            raise RepoError(422, "token_missing", "a token is needed to read the repository")
        if await self.find(user_id, name):
            raise RepoError(409, "name_taken", "a repository with this name exists")
        reader = GitHubReader.for_repo(self.settings, token.strip(), full)
        try:
            state = await read_state(reader)
        except GitHubError as exc:
            raise RepoError(422, exc.code, exc.message) from exc
        if mode == "test-branch" and not state["can_push"]:
            raise RepoError(
                422, "token_cannot_push", "mode test-branch needs a token that may push there"
            )
        row = DevRepo(
            user_id=user_id,
            name=name.strip(),
            full_name=full,
            default_branch=state["default_branch"],
            mode=mode,
            test_template=template,
            token_enc=self.box.encrypt(token.strip()),
            last_state=state,
            last_checked_at=datetime.now(UTC),
        )
        async with self.sessionmaker() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row

    async def update(
        self,
        user_id: uuid.UUID,
        ref: str,
        *,
        mode: str | None = None,
        template: str | None = None,
        token: str | None = None,
    ) -> DevRepo:
        """Change the mode, the test template or the token of a registered repository (the name
        and the address stay: that is another repository). A new token is read once like on
        adding; a change to ``test-branch`` needs a token that may push."""
        row = await self.find(user_id, ref)
        if row is None:
            raise RepoError(404, "not_found", "unknown repository")
        new_mode = mode if mode is not None else row.mode
        new_template = template if template is not None else row.test_template
        check_fields(row.name, new_mode, new_template)
        new_token = token.strip() if token is not None else ""
        if token is not None and not new_token:
            raise RepoError(422, "token_missing", "the new token is empty")
        reader = (
            GitHubReader.for_repo(self.settings, new_token, row.full_name)
            if new_token
            else self.reader(row)
        )
        try:
            state = await read_state(reader)
        except GitHubError as exc:
            raise RepoError(422, exc.code, exc.message) from exc
        if new_mode == "test-branch" and not state["can_push"]:
            raise RepoError(
                422, "token_cannot_push", "mode test-branch needs a token that may push there"
            )
        async with self.sessionmaker() as db:
            fresh = await db.get(DevRepo, row.id)
            if fresh is None or fresh.user_id != user_id:
                raise RepoError(404, "not_found", "unknown repository")
            fresh.mode = new_mode
            fresh.test_template = new_template
            if new_token:
                fresh.token_enc = self.box.encrypt(new_token)
            fresh.default_branch = state["default_branch"]
            fresh.last_state = state
            fresh.last_checked_at = datetime.now(UTC)
            await db.commit()
            await db.refresh(fresh)
            return fresh

    async def refresh(self, user_id: uuid.UUID, ref: str) -> DevRepo:
        row = await self.find(user_id, ref)
        if row is None:
            raise RepoError(404, "not_found", "unknown repository")
        try:
            state: dict[str, Any] = await read_state(self.reader(row))
        except GitHubError as exc:
            state = {"error": exc.code, "message": exc.message}
        async with self.sessionmaker() as db:
            fresh = await db.get(DevRepo, row.id)
            if fresh is None or fresh.user_id != user_id:
                raise RepoError(404, "not_found", "unknown repository")
            fresh.last_state = state
            fresh.last_checked_at = datetime.now(UTC)
            if "default_branch" in state:
                fresh.default_branch = state["default_branch"]
            await db.commit()
            await db.refresh(fresh)
            return fresh

    async def remove(self, user_id: uuid.UUID, ref: str) -> None:
        row = await self.find(user_id, ref)
        if row is None:
            raise RepoError(404, "not_found", "unknown repository")
        async with self.sessionmaker() as db:
            fresh = await db.get(DevRepo, row.id)
            if fresh is not None and fresh.user_id == user_id:
                await db.delete(fresh)
                await db.commit()

    # --- local repositories (D76) ------------------------------------------------------------

    async def dev_nodes(self) -> Sequence[Node]:
        """The dev nodes that can hold local repositories: role dev, ``workshop.manage`` allowed."""
        async with self.sessionmaker() as db:
            rows = await db.scalars(select(Node).order_by(Node.name))
            return [
                n
                for n in rows
                if "dev" in (n.config or {}).get("roles", [])
                and "workshop.manage" in (n.config or {}).get("allowed_actions", [])
            ]

    async def pick_node(self, node_id: uuid.UUID | None) -> Node:
        nodes = await self.dev_nodes()
        if node_id is not None:
            found = next((n for n in nodes if n.id == node_id), None)
            if found is None:
                raise RepoError(422, "no_dev_node", "that node cannot hold local repositories")
            return found
        if len(nodes) == 1:
            return nodes[0]
        if not nodes:
            raise RepoError(409, "no_dev_node", "no dev node can hold local repositories")
        raise RepoError(422, "choose_node", "several dev nodes: choose one")

    async def _manage(
        self,
        caller: Any,
        node_id: uuid.UUID | None,
        capability: str,
        tool_input: dict[str, Any],
        repo: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from topcoat_hub.tools import manage

        if self.tools is None or node_id is None:
            raise RepoError(409, "no_dev_node", "the repository's dev node is not known")
        try:
            return await manage(self.tools, caller, node_id, capability, tool_input, repo)
        except ToolError as exc:
            raise RepoError(exc.status, exc.code, exc.message) from exc

    def _remote(self, row: DevRepo) -> dict[str, Any]:
        """The address and token of an external repository for one call of the node."""
        try:
            token = self.box.decrypt(row.token_enc)
        except SecretError as exc:
            raise RepoError(409, "token_unreadable", str(exc)) from exc
        return {
            "url": f"https://github.com/{row.full_name}.git",
            "branch": row.default_branch,
            "token": token,
        }

    async def _taken(self, user_id: uuid.UUID, name: str) -> bool:
        """The exact name is used (``find`` also matches the repository part of owner/name)."""
        return any(r.name == name.strip() for r in await self.list(user_id))

    async def _local(self, user_id: uuid.UUID, ref: str) -> DevRepo:
        row = await self.find(user_id, ref)
        if row is None:
            raise RepoError(404, "not_found", "unknown repository")
        if row.kind != "local":
            raise RepoError(409, "not_local", f"{row.name} is not a local repository")
        return row

    async def _save(self, row: DevRepo) -> DevRepo:
        async with self.sessionmaker() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row

    async def create_local(
        self,
        caller: Any,
        *,
        name: str,
        node_id: uuid.UUID | None,
        branch: str,
        template: str,
        title: str = "",
        description: str = "",
    ) -> DevRepo:
        """A new repository in the workshop of a dev node, with a first commit."""
        check_fields(name, "test-branch", template)
        if await self._taken(caller.user_id, name):
            raise RepoError(409, "name_taken", "a repository with this name exists")
        node = await self.pick_node(node_id)
        out = await self._manage(
            caller,
            node.id,
            "workshop.create",
            {"repo": name, "branch": branch or "main", "title": title, "description": description},
        )
        return await self._save(
            DevRepo(
                user_id=caller.user_id,
                kind="local",
                node_id=node.id,
                name=name,
                full_name="",
                default_branch=str(out.get("default_branch") or branch or "main"),
                mode="test-branch",
                test_template=template,
                token_enc="",
                last_state={"created": True, "commit": out.get("commit")},
                last_checked_at=datetime.now(UTC),
            )
        )

    async def clone_local(
        self, caller: Any, ref: str, *, name: str, node_id: uuid.UUID | None
    ) -> DevRepo:
        """A local clone of an external repository, for editing and testing in the workshop; the
        external one stays its upstream (where it is published to)."""
        source = await self.find(caller.user_id, ref)
        if source is None:
            raise RepoError(404, "not_found", "unknown repository")
        if source.kind != "external":
            raise RepoError(409, "not_external", "only an external repository is cloned")
        check_fields(name, "test-branch", source.test_template)
        if await self._taken(caller.user_id, name):
            raise RepoError(409, "name_taken", "a repository with this name exists")
        node = await self.pick_node(node_id)
        out = await self._manage(
            caller, node.id, "workshop.clone", {"repo": name}, self._remote(source)
        )
        return await self._save(
            DevRepo(
                user_id=caller.user_id,
                kind="local",
                node_id=node.id,
                upstream_id=source.id,
                name=name,
                full_name="",
                default_branch=str(out.get("default_branch") or source.default_branch),
                mode="test-branch",
                test_template=source.test_template,
                token_enc="",
                last_state={"cloned_from": source.full_name, "branches": out.get("branches")},
                last_checked_at=datetime.now(UTC),
            )
        )

    async def branches(self, caller: Any, ref: str) -> dict[str, Any]:
        """The branches of a local repository, as its dev node sees them (read-only)."""
        row = await self._local(caller.user_id, ref)
        if self.tools is None or row.node_id is None:
            raise RepoError(409, "no_dev_node", "the repository's dev node is not known")
        try:
            run = await self.tools.invoke(caller, row.node_id, "repo.branches", {"repo": row.name})
        except ToolError as exc:
            raise RepoError(exc.status, exc.code, exc.message) from exc
        if not run.get("ok"):
            err = run.get("error") or {}
            raise RepoError(502, str(err.get("code", "error")), str(err.get("message", "failed")))
        out = run.get("output")
        return out if isinstance(out, dict) else {}

    async def create_branch(self, caller: Any, ref: str, name: str, start: str) -> dict[str, Any]:
        row = await self._local(caller.user_id, ref)
        body: dict[str, Any] = {"repo": row.name, "name": name}
        if start:
            body["from"] = start
        return await self._manage(caller, row.node_id, "workshop.branch", body)

    async def merge(self, caller: Any, ref: str, branch: str, message: str) -> dict[str, Any]:
        """Take a branch into the default branch: the person's click after seeing the change."""
        row = await self._local(caller.user_id, ref)
        body: dict[str, Any] = {"repo": row.name, "branch": branch, "into": row.default_branch}
        if message:
            body["message"] = message
        return await self._manage(caller, row.node_id, "workshop.merge", body)

    async def _upstream(self, caller: Any, row: DevRepo) -> DevRepo:
        if row.upstream_id is None:
            raise RepoError(409, "no_upstream", f"{row.name} is not connected to a remote")
        up = next((r for r in await self.list(caller.user_id) if r.id == row.upstream_id), None)
        if up is None:
            raise RepoError(409, "no_upstream", "the remote of this repository is gone")
        return up

    async def publish(self, caller: Any, ref: str, branch: str) -> dict[str, Any]:
        """Push a branch to the upstream (never forced): the person's click."""
        row = await self._local(caller.user_id, ref)
        up = await self._upstream(caller, row)
        return await self._manage(
            caller,
            row.node_id,
            "workshop.publish",
            {"repo": row.name, "branch": branch},
            self._remote(up),
        )

    async def pull(self, caller: Any, ref: str, branch: str) -> dict[str, Any]:
        """Bring a branch up to date with the upstream (fast-forward only)."""
        row = await self._local(caller.user_id, ref)
        up = await self._upstream(caller, row)
        return await self._manage(
            caller,
            row.node_id,
            "workshop.pull",
            {"repo": row.name, "branch": branch or row.default_branch},
            self._remote(up),
        )

    async def connect(self, caller: Any, ref: str, upstream: str | None) -> DevRepo:
        """Connect a local repository to an external one (its remote), or cut it off (None)."""
        row = await self._local(caller.user_id, ref)
        up_id: uuid.UUID | None = None
        if upstream:
            up = await self.find(caller.user_id, upstream)
            if up is None or up.kind != "external":
                raise RepoError(422, "not_external", "connect it to an external repository")
            up_id = up.id
        async with self.sessionmaker() as db:
            fresh = await db.get(DevRepo, row.id)
            if fresh is None or fresh.user_id != caller.user_id:
                raise RepoError(404, "not_found", "unknown repository")
            fresh.upstream_id = up_id
            await db.commit()
            await db.refresh(fresh)
            return fresh

    async def remove_local(self, caller: Any, ref: str) -> None:
        """Delete a local repository in its workshop and here (it cannot be undone)."""
        row = await self._local(caller.user_id, ref)
        await self._manage(
            caller, row.node_id, "workshop.remove", {"repo": row.name, "confirm": True}
        )
        await self.remove(caller.user_id, row.name)
