"""Release store of the signed update channel (E2-06, ARCHITECTURE D50).

The hub only *distributes*: builds are signed on the release machine (``topcoat-sign``), the
hub keeps the manifest, the signature and the program file per ``channel / os-arch / version``
and hands them to nodes. A node (and its updater) verifies the signature against the keys built
into its own program, so a hub that is wrong or taken over cannot make a node run unsigned code.
If ``TOPCOAT_UPDATE_PUBKEYS`` is set, the hub additionally refuses uploads that do not verify, so
that a mistake is found at upload time and not on the node.

Layout below the store directory::

    <channel>/<os>-<arch>/<version>/{manifest.json, manifest.sig, topcoat-node}
    <channel>/<os>-<arch>/.incoming-<version>/...     an upload that is not complete yet
"""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import secrets
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

log = logging.getLogger(__name__)

PRODUCT = "topcoat-node"
FORMAT_VERSION = 1
SIG_SCHEME = "topcoat-sig-v1"
SIGNED_PREFIX = b"topcoat-update-v1\n"
MAX_MANIFEST_BYTES = 16 << 10
MAX_BINARY_BYTES = 256 << 20
MAX_KEYS = 2

BINARY_FILE = "topcoat-node"
MANIFEST_FILE = "manifest.json"
SIG_FILE = "manifest.sig"

CHANNEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
PLAT_RE = re.compile(r"^[a-z0-9_]{1,16}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]{1,32})?$"
)

_FIELDS: dict[str, type] = {
    "format": int,
    "product": str,
    "channel": str,
    "version": str,
    "os": str,
    "arch": str,
    "size": int,
    "sha256": str,
    "created": str,
}


class UpdateError(Exception):
    """A refusal with an HTTP status and a stable code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def version_key(version: str) -> tuple[int, int, int, int, str]:
    """Sort key with the order of the node: a pre-release is older than its release."""
    m = VERSION_RE.match(version)
    if m is None:
        raise ValueError(f"version {version!r} is not MAJOR.MINOR.PATCH[-pre]")
    pre = (m.group(4) or "").removeprefix("-")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if pre else 1, pre)


def is_newer(candidate: str, installed: str | None) -> bool:
    """Strictly newer; an installed version that is no release number (a dev build) counts as
    0.0.0, like on the node."""
    try:
        have = version_key(installed or "")
    except ValueError:
        have = (0, 0, 0, 0, "")
    return version_key(candidate) > have


def node_channel(config: dict[str, Any] | None) -> str:
    """The channel a node follows (``config.update.channel``, default ``stable``)."""
    update = (config or {}).get("update")
    channel = update.get("channel") if isinstance(update, dict) else None
    return channel if isinstance(channel, str) and channel else "stable"


def key_id(public: bytes) -> str:
    return hashlib.sha256(public).hexdigest()[:16]


def parse_keys(text: str) -> list[tuple[str, bytes]]:
    """``TOPCOAT_UPDATE_PUBKEYS``: comma-separated base64 ed25519 public keys (at most two)."""
    keys: list[tuple[str, bytes]] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            raw = base64.b64decode(part, validate=True)
        except (binascii.Error, ValueError):
            raw = b""
        if len(raw) != 32:
            raise ValueError(f"update key {part!r} is not a base64 ed25519 public key")
        keys.append((key_id(raw), raw))
    if len(keys) > MAX_KEYS:
        raise ValueError(f"at most {MAX_KEYS} update keys are allowed, got {len(keys)}")
    return keys


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate field {key!r}")
        out[key] = value
    return out


def parse_manifest(text: str) -> dict[str, Any]:
    """Read a manifest as strictly as the node does."""
    raw = text.encode()
    if not 0 < len(raw) <= MAX_MANIFEST_BYTES:
        raise UpdateError(400, "bad_manifest", f"manifest must be 1 to {MAX_MANIFEST_BYTES} bytes")
    try:
        m = json.loads(text, object_pairs_hook=_no_duplicates)
    except ValueError as exc:
        raise UpdateError(400, "bad_manifest", f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(m, dict) or set(m) != set(_FIELDS):
        raise UpdateError(400, "bad_manifest", f"manifest fields must be exactly {sorted(_FIELDS)}")
    for name, kind in _FIELDS.items():
        value = m[name]
        if not isinstance(value, kind) or isinstance(value, bool):
            raise UpdateError(400, "bad_manifest", f"manifest field {name!r} has the wrong type")
    problem = None
    if m["format"] != FORMAT_VERSION:
        problem = f"format {m['format']} is not supported"
    elif m["product"] != PRODUCT:
        problem = f"product {m['product']!r} is not {PRODUCT!r}"
    elif not CHANNEL_RE.match(m["channel"]):
        problem = f"channel {m['channel']!r} is not valid"
    elif not VERSION_RE.match(m["version"]):
        problem = f"version {m['version']!r} is not MAJOR.MINOR.PATCH[-pre]"
    elif not (PLAT_RE.match(m["os"]) and PLAT_RE.match(m["arch"])):
        problem = "platform is not valid"
    elif not 1 <= m["size"] <= MAX_BINARY_BYTES:
        problem = f"size must be between 1 and {MAX_BINARY_BYTES}"
    elif not SHA256_RE.match(m["sha256"]):
        problem = "sha256 must be 64 lower-case hex digits"
    if problem:
        raise UpdateError(400, "bad_manifest", f"manifest: {problem}")
    return m


@dataclass(frozen=True)
class Release:
    channel: str
    os: str
    arch: str
    version: str
    size: int
    sha256: str
    created: str
    directory: Path
    manifest_text: str
    signature: str

    @property
    def binary(self) -> Path:
        return self.directory / BINARY_FILE

    def public(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "os": self.os,
            "arch": self.arch,
            "version": self.version,
            "size": self.size,
            "sha256": self.sha256,
            "created": self.created,
        }


class UpdateStore:
    """The directory of releases. ``root=None`` switches the channel off."""

    def __init__(
        self,
        root: Path | None,
        keys: list[tuple[str, bytes]] | None = None,
        keep: int = 2,
        max_bytes: int = MAX_BINARY_BYTES,
    ) -> None:
        self.root = root
        self.keys = keys or []
        self.keep = max(1, keep)
        self.max_bytes = min(max_bytes, MAX_BINARY_BYTES)
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.root is not None

    @property
    def key_ids(self) -> list[str]:
        return [k for k, _ in self.keys]

    def _root(self) -> Path:
        if self.root is None:
            raise UpdateError(
                503, "updates_disabled", "the update store is off: set TOPCOAT_UPDATE_DIR"
            )
        return self.root

    def _dir(self, channel: str, os_name: str, arch: str, version: str) -> Path:
        # Every part is checked against its pattern, so none can climb out of the store.
        if not (
            CHANNEL_RE.match(channel)
            and PLAT_RE.match(os_name)
            and PLAT_RE.match(arch)
            and VERSION_RE.match(version)
        ):
            raise UpdateError(400, "bad_request", "channel, platform or version is not valid")
        return self._root() / channel / f"{os_name}-{arch}" / version

    # --- reading -------------------------------------------------------------------

    def _load(self, directory: Path) -> Release | None:
        try:
            text = (directory / MANIFEST_FILE).read_text(encoding="utf-8")
            signature = (directory / SIG_FILE).read_text(encoding="utf-8").strip()
            size = (directory / BINARY_FILE).stat().st_size
            m = parse_manifest(text)
        except (OSError, UpdateError, ValueError):
            return None
        if size != m["size"]:
            return None
        return Release(
            channel=m["channel"],
            os=m["os"],
            arch=m["arch"],
            version=m["version"],
            size=m["size"],
            sha256=m["sha256"],
            created=m["created"],
            directory=directory,
            manifest_text=text,
            signature=signature,
        )

    def releases(self) -> list[Release]:
        """Every complete release, newest first per channel and platform."""
        if self.root is None or not self.root.is_dir():
            return []
        found: list[Release] = []
        for plat_dir in sorted(self.root.glob("*/*")):
            if not plat_dir.is_dir():
                continue
            for d in plat_dir.iterdir():
                if d.name.startswith(".") or not d.is_dir() or not VERSION_RE.match(d.name):
                    continue
                rel = self._load(d)
                # the directory name must agree with what the signed manifest says
                if (
                    rel is not None
                    and rel.version == d.name
                    and (rel.channel, f"{rel.os}-{rel.arch}")
                    == (plat_dir.parent.name, plat_dir.name)
                ):
                    found.append(rel)
        found.sort(key=lambda r: (r.channel, r.os, r.arch, version_key(r.version)), reverse=True)
        return found

    def get(self, channel: str, os_name: str, arch: str, version: str) -> Release | None:
        d = self._dir(channel, os_name, arch, version)
        rel = self._load(d) if d.is_dir() else None
        return rel if rel is not None and rel.version == version else None

    def latest(self, channel: str, os_name: str, arch: str) -> Release | None:
        if self.root is None or not (
            CHANNEL_RE.match(channel) and PLAT_RE.match(os_name) and PLAT_RE.match(arch)
        ):
            return None
        mine = [r for r in self.releases() if (r.channel, r.os, r.arch) == (channel, os_name, arch)]
        return max(mine, key=lambda r: version_key(r.version), default=None)

    # --- checking ------------------------------------------------------------------

    def check_signature(self, manifest_text: str, signature: str) -> None:
        """With keys configured the hub refuses what no key signed (the node checks anyway)."""
        if not self.keys:
            return
        fields = signature.split()
        if len(fields) != 3 or fields[0] != SIG_SCHEME:
            raise UpdateError(400, "bad_signature", f"signature must be a {SIG_SCHEME} line")
        try:
            sig = base64.b64decode(fields[2], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise UpdateError(400, "bad_signature", "signature is malformed") from exc
        for kid, raw in self.keys:
            if kid != fields[1]:
                continue
            try:
                Ed25519PublicKey.from_public_bytes(raw).verify(
                    sig, SIGNED_PREFIX + manifest_text.encode()
                )
            except InvalidSignature as exc:
                raise UpdateError(
                    400, "bad_signature", "signature does not match the manifest"
                ) from exc
            return
        raise UpdateError(400, "bad_signature", f"key {fields[1]} is not one of the update keys")

    # --- writing -------------------------------------------------------------------

    def _incoming(self, final: Path) -> Path:
        return final.parent / f".incoming-{final.name}"

    async def put_manifest(
        self, channel: str, version: str, plat: str, manifest_text: str, signature: str
    ) -> None:
        os_name, _, arch = plat.partition("-")
        final = self._dir(channel, os_name, arch, version)
        m = parse_manifest(manifest_text)
        if (m["channel"], m["version"], f"{m['os']}-{m['arch']}") != (channel, version, plat):
            raise UpdateError(
                400, "mismatch", "the manifest names another channel, version or platform"
            )
        if m["size"] > self.max_bytes:
            raise UpdateError(
                413, "too_large", f"the program may be at most {self.max_bytes} bytes"
            )
        signature = signature.strip()
        self.check_signature(manifest_text, signature)
        async with self._lock:
            if final.is_dir():
                raise UpdateError(409, "exists", f"{channel} {version} {plat} is published already")
            incoming = self._incoming(final)
            shutil.rmtree(incoming, ignore_errors=True)
            incoming.mkdir(parents=True)
            (incoming / MANIFEST_FILE).write_text(manifest_text, encoding="utf-8")
            (incoming / SIG_FILE).write_text(signature + "\n", encoding="utf-8")

    async def put_binary(
        self, channel: str, version: str, plat: str, chunks: AsyncIterator[bytes]
    ) -> Release:
        os_name, _, arch = plat.partition("-")
        final = self._dir(channel, os_name, arch, version)
        incoming = self._incoming(final)
        try:
            m = parse_manifest((incoming / MANIFEST_FILE).read_text(encoding="utf-8"))
        except (OSError, UpdateError) as exc:
            raise UpdateError(
                409, "no_manifest", "upload the manifest of this build first"
            ) from exc
        tmp = incoming / f"{BINARY_FILE}.{secrets.token_hex(4)}.tmp"
        digest = hashlib.sha256()
        total = 0
        try:
            with tmp.open("wb") as f:
                async for chunk in chunks:
                    total += len(chunk)
                    if total > m["size"]:
                        raise UpdateError(400, "size_mismatch", "more bytes than the manifest says")
                    digest.update(chunk)
                    await asyncio.to_thread(f.write, chunk)
            if total != m["size"]:
                raise UpdateError(
                    400, "size_mismatch", f"got {total} bytes, the manifest says {m['size']}"
                )
            if digest.hexdigest() != m["sha256"]:
                raise UpdateError(400, "hash_mismatch", "the program does not match the SHA-256")
            async with self._lock:
                if final.is_dir():
                    raise UpdateError(409, "exists", "this build was published meanwhile")
                tmp.replace(incoming / BINARY_FILE)
                incoming.rename(final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        rel = self._load(final)
        if rel is None:  # cannot happen unless the disk misbehaves
            raise UpdateError(500, "store_error", "the release could not be read back")
        self.prune(channel, os_name, arch)
        log.info("update published: %s %s %s", channel, version, plat)
        return rel

    def prune(self, channel: str, os_name: str, arch: str) -> None:
        """Keep the newest releases of one channel and platform; the rest is the rollback
        window of the past."""
        mine = [r for r in self.releases() if (r.channel, r.os, r.arch) == (channel, os_name, arch)]
        mine.sort(key=lambda r: version_key(r.version), reverse=True)
        for old in mine[self.keep :]:
            shutil.rmtree(old.directory, ignore_errors=True)
            log.info("update pruned: %s %s %s-%s", old.channel, old.version, old.os, old.arch)

    async def delete(self, channel: str, version: str, plat: str) -> bool:
        os_name, _, arch = plat.partition("-")
        final = self._dir(channel, os_name, arch, version)
        async with self._lock:
            gone = final.is_dir()
            shutil.rmtree(final, ignore_errors=True)
            shutil.rmtree(self._incoming(final), ignore_errors=True)
        return gone
