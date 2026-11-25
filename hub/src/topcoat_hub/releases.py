"""Releases built inside Topcoat, signed in the admin's browser (NS-01, ARCHITECTURE D80).

The admin starts a build on the Releases page; the hub gives it to the node of role ``build``, which
fetches a pinned commit, builds, and uploads the artifacts to the hub. The build service never
signs. The hub shows commit, changes since the last release and checksums; the admin chooses the key
file in the page, the browser signs the manifests, and only the signatures come back. The hub
checks them against the configured update keys and publishes through the update store. No agent, no
API key and no schedule reaches any of this: the routes want the signed-in admin, and the steps on
the node run only through ``build.manage``.
"""

import asyncio
import hashlib
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.models import Node, ReleaseBuild
from topcoat_hub.tools import BUILD_MANAGE, HUB_MANAGE, Caller, ToolError, ToolService, manage
from topcoat_hub.updates import (
    CHANNEL_RE,
    PLAT_RE,
    PRODUCT,
    UpdateError,
    UpdateStore,
    version_key,
)

log = logging.getLogger(__name__)

KINDS = ("node_release", "hub_image")
SOURCES = ("release_repo", "dev_mirror")
ACTIVE = ("requested", "building")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]{1,32})?$"
)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}$")
MAX_ARTIFACT_BYTES = 512 << 20
MAX_LOG_CHARS = 60000
MAX_CHANGES_CHARS = 4000
CREATED_SKEW = timedelta(hours=2)
CREATED_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# what must never reach a stored log (the node filters first; this is the second pair of eyes)
_SECRETS = [
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
    ),
    re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)\b(bearer|token|authorization)(\s*[:=]\s*|\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key)(\s*[:=]\s*)\S{6,}"),
    re.compile(r"topcoat-update-key-v1 [A-Za-z0-9+/=]{40,}"),
]


def mask_secrets(text: str) -> str:
    """The log with everything that looks like a key, a token or a password replaced."""
    for pattern in _SECRETS:
        text = pattern.sub(
            lambda m: (m.group(1) + m.group(2) if m.lastindex == 2 else "") + "[masked]", text
        )
    return text


class ReleaseError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def build_view(b: ReleaseBuild) -> dict[str, Any]:
    def when(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "id": str(b.id),
        "kind": b.kind,
        "source": b.source,
        "ref": b.ref,
        "commit": b.commit,
        "version": b.version,
        "state": b.state,
        "node": b.node_name,
        "changes": b.changes,
        "artifacts": b.artifacts or [],
        "smoke": b.smoke,
        "rollout": b.rollout or [],
        "log": b.log,
        "error": b.error,
        "channel": b.channel,
        "key_id": b.key_id,
        "created_at": when(b.created_at),
        "started_at": when(b.started_at),
        "finished_at": when(b.finished_at),
        "published_at": when(b.published_at),
    }


def manifest_text(
    *, channel: str, version: str, os_name: str, arch: str, size: int, sha256: str, created: str
) -> str:
    """The manifest exactly as ``topcoat-sign`` writes it (Go's ``json.Marshal`` of the struct:
    this field order, no spaces). It is signed as written, so it is built here once."""
    return json.dumps(
        {
            "format": 1,
            "product": PRODUCT,
            "channel": channel,
            "version": version,
            "os": os_name,
            "arch": arch,
            "size": size,
            "sha256": sha256,
            "created": created,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


class ReleaseService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        tools: ToolService,
        updates: UpdateStore,
        root: Path | None,
        step_timeout: float = 1800.0,
        rollout_node: str = "",
    ) -> None:
        self.rollout_node = rollout_node
        self.sessionmaker = sessionmaker
        self.tools = tools
        self.updates = updates
        self.root = root
        self.step_timeout = step_timeout
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def _dir(self, build_id: uuid.UUID) -> Path:
        if self.root is None:
            raise ReleaseError(503, "disabled", "releases are switched off on this hub")
        return self.root / str(build_id)

    # --- reading ----------------------------------------------------------------------------

    async def recent(self, user_id: uuid.UUID, limit: int = 50) -> list[ReleaseBuild]:
        async with self.sessionmaker() as db:
            rows = await db.scalars(
                select(ReleaseBuild)
                .where(ReleaseBuild.user_id == user_id)
                .order_by(ReleaseBuild.created_at.desc())
                .limit(limit)
            )
            return list(rows)

    async def get(self, user_id: uuid.UUID, build_id: uuid.UUID) -> ReleaseBuild:
        async with self.sessionmaker() as db:
            return await _own(db, user_id, build_id)

    async def build_nodes(self) -> list[dict[str, Any]]:
        """The nodes that can build: role ``build``, the action allowed."""
        async with self.sessionmaker() as db:
            nodes = list(await db.scalars(select(Node)))
        return [
            {"id": str(n.id), "name": n.name, "state": n.state}
            for n in nodes
            if "build" in (n.config or {}).get("roles", [])
            and BUILD_MANAGE in (n.config or {}).get("allowed_actions", [])
        ]

    # --- starting ---------------------------------------------------------------------------

    async def start(
        self,
        user_id: uuid.UUID,
        *,
        kind: str,
        ref: str,
        version: str | None,
        source: str,
        run: bool = True,
    ) -> ReleaseBuild:
        """Record a build and give it to the build node. One at a time: a second one is refused
        while another is on its way."""
        if not self.enabled:
            raise ReleaseError(503, "disabled", "releases are switched off on this hub")
        if kind not in KINDS or source not in SOURCES:
            raise ReleaseError(422, "invalid", "unknown kind or source")
        if not REF_RE.match(ref) or ".." in ref:
            raise ReleaseError(422, "invalid", "that is not a commit, branch or tag")
        if kind == "node_release" and not (version and VERSION_RE.match(version)):
            raise ReleaseError(422, "invalid", "a node release needs a version like 0.3.0")
        if kind != "node_release":
            version = None
        nodes = [n for n in await self.build_nodes() if n["state"] != "offline"]
        if not nodes:
            raise ReleaseError(409, "no_build_node", "no build node is connected")
        async with self.sessionmaker() as db:
            busy = await db.scalar(
                select(ReleaseBuild.id).where(ReleaseBuild.state.in_(ACTIVE)).limit(1)
            )
            if busy is not None:
                raise ReleaseError(409, "busy", "a build is running: one at a time")
            build = ReleaseBuild(
                id=uuid.uuid4(),
                user_id=user_id,
                application="admin",
                kind=kind,
                source=source,
                ref=ref,
                version=version,
                state="requested",
                node_id=uuid.UUID(nodes[0]["id"]),
                node_name=nodes[0]["name"],
            )
            db.add(build)
            await db.commit()
            await db.refresh(build)
        if run:
            task = asyncio.create_task(self.run(user_id, build.id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return build

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- running ----------------------------------------------------------------------------

    async def _step(
        self, user_id: uuid.UUID, build: ReleaseBuild, capability: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        """One named step on the build node, as the signed-in admin who started the build."""
        if build.node_id is None:
            raise ReleaseError(409, "no_build_node", "the build has no node")
        out = await asyncio.wait_for(
            manage(
                self.tools,
                Caller(user_id, True, "gui"),
                build.node_id,
                capability,
                tool_input,
                action=BUILD_MANAGE,
            ),
            self.step_timeout,
        )
        return out

    async def run(self, user_id: uuid.UUID, build_id: uuid.UUID) -> None:
        """The steps of one build. Whatever goes wrong ends it as ``failed`` with the reason; a
        build never stays ``building`` after this returns."""
        try:
            await self._run(user_id, build_id)
        except asyncio.CancelledError:
            await self._finish(build_id, "failed", error="the hub stopped while it ran")
            raise
        except ToolError as exc:
            await self._finish(build_id, "failed", error=f"{exc.code}: {exc.message}"[:2000])
        except TimeoutError:
            await self._finish(build_id, "failed", error="a build step took too long")
        except ReleaseError as exc:
            await self._finish(build_id, "failed", error=exc.message)
        except Exception:
            log.exception("release build %s failed unexpectedly", build_id)
            await self._finish(build_id, "failed", error="the build failed unexpectedly")

    async def _run(self, user_id: uuid.UUID, build_id: uuid.UUID) -> None:
        async with self.sessionmaker() as db:
            build = await _own(db, user_id, build_id)
            build.state, build.started_at = "building", datetime.now(UTC)
            await db.commit()
            await db.refresh(build)
            db.expunge(build)
        logs: list[str] = []

        fetched = await self._step(
            user_id, build, "build.fetch", {"ref": build.ref, "source": build.source}
        )
        commit = str(fetched.get("commit", ""))
        if not COMMIT_RE.match(commit):
            raise ReleaseError(502, "bad_output", "the build node did not name a commit")
        logs.append(str(fetched.get("log", "")))
        await self._save(
            build_id,
            commit=commit,
            changes=mask_secrets(str(fetched.get("changes", "")))[:MAX_CHANGES_CHARS],
        )

        if build.kind == "node_release":
            out = await self._step(
                user_id,
                build,
                "build.node_release",
                {"commit": commit, "version": build.version, "build_id": str(build.id)},
            )
            logs.append(str(out.get("log", "")))
            await self._check_artifacts(build_id, out.get("artifacts"))
        else:
            out = await self._step(
                user_id, build, "build.hub_image", {"commit": commit, "build_id": str(build.id)}
            )
            logs.append(str(out.get("log", "")))
            image, digest = str(out.get("image", "")), str(out.get("digest", ""))
            if not IMAGE_RE.match(image) or not DIGEST_RE.match(digest):
                raise ReleaseError(502, "bad_output", "the build node named no image and digest")
            smoke = await self._step(user_id, build, "build.smoke_test", {"image": image})
            logs.append(str(smoke.get("log", "")))
            await self._save(
                build_id,
                artifacts=[
                    {
                        "kind": "image",
                        "name": image,
                        "digest": digest,
                        "size": int(out.get("size") or 0),
                    }
                ],
                smoke={
                    "ok": bool(smoke.get("ok")),
                    "summary": str(smoke.get("summary", ""))[:2000],
                },
            )
            if not smoke.get("ok"):
                raise ReleaseError(422, "smoke_failed", "the smoke test of the image failed")
        await self._save(build_id, log=mask_secrets("\n".join(logs))[-MAX_LOG_CHARS:])
        await self._finish(build_id, "built")

    async def _check_artifacts(self, build_id: uuid.UUID, reported: Any) -> None:
        """What the node says it built must be what the hub received, file by file."""
        if not isinstance(reported, list) or not reported:
            raise ReleaseError(502, "bad_output", "the build node reported no artifacts")
        async with self.sessionmaker() as db:
            row = await db.get(ReleaseBuild, build_id)
            received = {a["name"]: a for a in (row.artifacts if row else [])}
        said: dict[str, dict[str, Any]] = {}
        for item in reported:
            if not isinstance(item, dict) or not NAME_RE.match(str(item.get("name", ""))):
                raise ReleaseError(502, "bad_output", "an artifact has no valid name")
            said[item["name"]] = item
        if set(said) != set(received):
            raise ReleaseError(
                422,
                "artifact_mismatch",
                f"reported {sorted(said)}, received {sorted(received)}",
            )
        for name, item in said.items():
            got = received[name]
            if item.get("sha256") != got["sha256"] or item.get("size") != got["size"]:
                raise ReleaseError(
                    422, "artifact_mismatch", f"{name}: the upload is not what the node reported"
                )

    async def _save(self, build_id: uuid.UUID, **fields: Any) -> None:
        async with self.sessionmaker() as db:
            row = await db.get(ReleaseBuild, build_id)
            if row is None:
                return
            for key, value in fields.items():
                setattr(row, key, value)
            await db.commit()

    async def _finish(self, build_id: uuid.UUID, state: str, error: str | None = None) -> None:
        await self._save(build_id, state=state, error=error, finished_at=datetime.now(UTC))

    # --- artifacts from the node ---------------------------------------------------------------

    async def receive_artifact(
        self,
        node: Node,
        build_id: uuid.UUID,
        name: str,
        os_name: str,
        arch: str,
        chunks: AsyncIterator[bytes],
    ) -> dict[str, Any]:
        """A file the build node uploads. Only the node the build was given to, only while the
        build runs; the size and the SHA-256 are the hub's own."""
        if not NAME_RE.match(name) or not PLAT_RE.match(os_name) or not PLAT_RE.match(arch):
            raise ReleaseError(422, "invalid", "name or platform is not valid")
        async with self.sessionmaker() as db:
            build = await db.get(ReleaseBuild, build_id)
        if build is None or build.node_id != node.id:
            raise ReleaseError(404, "not_found", "no such build for this node")
        if build.state != "building" or build.kind != "node_release":
            raise ReleaseError(409, "not_building", "this build takes no uploads now")
        directory = self._dir(build_id)
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".{name}.{uuid.uuid4().hex[:8]}.tmp"
        digest, total = hashlib.sha256(), 0
        try:
            with tmp.open("wb") as handle:
                async for chunk in chunks:
                    total += len(chunk)
                    if total > MAX_ARTIFACT_BYTES:
                        raise ReleaseError(413, "too_large", "the artifact is too large")
                    digest.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)
            if total == 0:
                raise ReleaseError(422, "empty", "the artifact is empty")
            tmp.replace(directory / name)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        entry = {
            "kind": "binary",
            "name": name,
            "os": os_name,
            "arch": arch,
            "size": total,
            "sha256": digest.hexdigest(),
        }
        async with self.sessionmaker() as db:
            row = await db.get(ReleaseBuild, build_id, with_for_update=True)
            if row is None:
                raise ReleaseError(404, "not_found", "no such build")
            row.artifacts = [a for a in row.artifacts if a["name"] != name] + [entry]
            await db.commit()
        return entry

    # --- signing and publishing --------------------------------------------------------------

    def manifests(self, build: ReleaseBuild, channel: str, created: str) -> list[dict[str, Any]]:
        """The manifests of a built node release, one per platform, as they will be signed."""
        if build.kind != "node_release" or build.state not in ("built", "published"):
            raise ReleaseError(409, "not_built", "only a built node release has manifests")
        if not CHANNEL_RE.match(channel):
            raise ReleaseError(422, "invalid", "that is not a channel name")
        out = []
        for a in sorted(build.artifacts, key=lambda x: (x["os"], x["arch"])):
            text = manifest_text(
                channel=channel,
                version=str(build.version),
                os_name=a["os"],
                arch=a["arch"],
                size=a["size"],
                sha256=a["sha256"],
                created=created,
            )
            out.append(
                {
                    "platform": f"{a['os']}-{a['arch']}",
                    "name": a["name"],
                    "size": a["size"],
                    "sha256": a["sha256"],
                    "manifest": text,
                }
            )
        return out

    @staticmethod
    def now_created() -> str:
        return datetime.now(UTC).strftime(CREATED_FORMAT)

    @staticmethod
    def check_created(created: str) -> None:
        try:
            when = datetime.strptime(created, CREATED_FORMAT).replace(tzinfo=UTC)
        except ValueError as exc:
            raise ReleaseError(
                422, "invalid", "created must look like 2026-10-29T12:00:00Z"
            ) from exc
        if abs(datetime.now(UTC) - when) > CREATED_SKEW:
            raise ReleaseError(422, "stale", "the time of the preview is too old: preview again")

    async def publish(
        self,
        user_id: uuid.UUID,
        build_id: uuid.UUID,
        *,
        channel: str,
        created: str,
        signatures: dict[str, str],
    ) -> ReleaseBuild:
        """Publish a built node release with the signatures the browser made. The hub rebuilds the
        manifests itself, so a signature only counts for exactly the bytes the page showed."""
        if not self.updates.enabled or not self.updates.keys:
            raise ReleaseError(
                409, "no_update_keys", "set TOPCOAT_UPDATE_PUBKEYS: the hub checks every signature"
            )
        self.check_created(created)
        build = await self.get(user_id, build_id)
        wanted = self.manifests(build, channel, created)
        if build.state == "published":
            raise ReleaseError(409, "published", "this build is published already")
        if set(signatures) != {m["platform"] for m in wanted}:
            raise ReleaseError(422, "signatures", "send one signature per platform, no more")
        taken = {
            f"{r.os}-{r.arch}"
            for r in self.updates.releases()
            if r.channel == channel and r.version == build.version
        }
        if taken:
            raise ReleaseError(
                409, "exists", f"{channel} {build.version} exists already for {sorted(taken)}"
            )
        key_ids: set[str] = set()
        for m in wanted:  # check all of them before anything is published
            line = signatures[m["platform"]].strip()
            try:
                self.updates.check_signature(m["manifest"], line)
            except UpdateError as exc:
                raise ReleaseError(422, "bad_signature", f"{m['platform']}: {exc.message}") from exc
            key_ids.add(line.split()[1])
        if len(key_ids) != 1:
            raise ReleaseError(422, "signatures", "all platforms are signed with one key")
        directory = self._dir(build_id)
        for m in wanted:
            path = directory / m["name"]
            if not path.is_file() or path.stat().st_size != m["size"]:
                raise ReleaseError(409, "artifact_gone", f"{m['name']} is not on the hub any more")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != m["sha256"]:
                raise ReleaseError(409, "artifact_changed", f"{m['name']} changed on disk")
        done: list[str] = []
        try:
            for m in wanted:
                await self.updates.put_manifest(
                    channel,
                    str(build.version),
                    m["platform"],
                    m["manifest"],
                    signatures[m["platform"]].strip(),
                )
                await self.updates.put_binary(
                    channel, str(build.version), m["platform"], _file_chunks(directory / m["name"])
                )
                done.append(m["platform"])
        except UpdateError as exc:
            for plat in done:  # a half published release helps nobody
                await self.updates.delete(channel, str(build.version), plat)
            raise ReleaseError(exc.status, exc.code, exc.message) from exc
        async with self.sessionmaker() as db:
            row = await _own(db, user_id, build_id)
            row.state, row.channel = "published", channel
            row.key_id, row.published_at = key_ids.pop(), datetime.now(UTC)
            await db.commit()
            await db.refresh(row)
            return row

    async def rollout(
        self, user_id: uuid.UUID, build_id: uuid.UUID, *, dry_run: bool
    ) -> ReleaseBuild:
        """Roll the hub out to a built and smoke-tested image: the admin's own click, separate
        from the build and from any signature. The production host's node checks that the image on
        the host has the digest that was built; the script keeps its own way back. A real run
        needs a dry run of the same image before it, so the checks have been seen first."""
        if not self.rollout_node:
            raise ReleaseError(409, "no_rollout_node", "set TOPCOAT_ROLLOUT_NODE")
        build = await self.get(user_id, build_id)
        if build.kind != "hub_image" or build.state != "built":
            raise ReleaseError(409, "not_built", "only a built hub image is rolled out")
        if not (build.smoke or {}).get("ok"):
            raise ReleaseError(409, "not_tested", "the smoke test of this image did not pass")
        image = next((a for a in build.artifacts if a.get("kind") == "image"), None)
        if image is None or not DIGEST_RE.match(str(image.get("digest", ""))):
            raise ReleaseError(409, "no_image", "the build has no image with a digest")
        if not dry_run and not any(r.get("dry_run") and r.get("ok") for r in (build.rollout or [])):
            raise ReleaseError(409, "dry_run_first", "run the checks (dry run) of this image first")
        async with self.sessionmaker() as db:
            node = await db.scalar(select(Node).where(Node.name == self.rollout_node))
        if node is None:
            raise ReleaseError(
                409, "no_rollout_node", f"there is no node named {self.rollout_node}"
            )
        try:
            out = await asyncio.wait_for(
                manage(
                    self.tools,
                    Caller(user_id, True, "gui"),
                    node.id,
                    "hub.update",
                    {"image": image["name"], "digest": image["digest"], "dry_run": dry_run},
                    action=HUB_MANAGE,
                ),
                self.step_timeout,
            )
            attempt = {"ok": bool(out.get("ok")), "output": str(out.get("output", ""))[-8000:]}
            if out.get("error"):
                attempt["error"] = str(out["error"])[:500]
        except ToolError as exc:
            attempt = {"ok": False, "output": "", "error": f"{exc.code}: {exc.message}"[:500]}
        except TimeoutError:
            attempt = {"ok": False, "output": "", "error": "the roll-out took too long"}
        attempt.update(at=datetime.now(UTC).isoformat(), node=node.name, dry_run=dry_run)
        attempt["output"] = mask_secrets(attempt["output"])
        async with self.sessionmaker() as db:
            row = await _own(db, user_id, build_id)
            row.rollout = [*(row.rollout or []), attempt]
            await db.commit()
            await db.refresh(row)
            return row

    def previous_version(self, channel: str) -> str | None:
        versions = [r.version for r in self.updates.releases() if r.channel == channel]
        return max(versions, key=version_key, default=None)


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as handle:
        while chunk := await asyncio.to_thread(handle.read, 1 << 20):
            yield chunk


async def _own(db: AsyncSession, user_id: uuid.UUID, build_id: uuid.UUID) -> ReleaseBuild:
    row = await db.scalar(
        select(ReleaseBuild).where(ReleaseBuild.id == build_id, ReleaseBuild.user_id == user_id)
    )
    if row is None:
        raise ReleaseError(404, "not_found", "no such build")
    return row
