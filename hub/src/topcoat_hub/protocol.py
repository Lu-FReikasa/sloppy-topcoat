"""Node <-> hub protocol helpers. The JSON Schemas in ``proto/`` are the source of truth."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

PROTOCOL_VERSION = 1
# The hub supports the current and the previous protocol version (docs/PROTOCOL.md): v0 nodes
# (the draft before the E2-01 freeze) are accepted for one more release.
SUPPORTED_VERSIONS = frozenset({0, 1})

MESSAGE_TYPES = (
    "hello",
    "config",
    "inventory",
    "heartbeat",
    "action",
    "action_result",
    "event",
    "queued_events",
    "error",
    "proxy_request",
    "proxy_chunk",
    "proxy_end",
    "proxy_status",
    "proxy_cancel",
)


class ProtocolError(ValueError):
    """A message did not match the schema."""


class SchemaSet:
    """All schemas of one protocol version, with ``$ref`` resolution between them."""

    def __init__(self, proto_dir: Path) -> None:
        resources = []
        self._by_name: dict[str, dict[str, Any]] = {}
        for path in sorted(proto_dir.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            name = path.name.removesuffix(".schema.json")
            self._by_name[name] = schema
            resources.append((schema["$id"], Resource.from_contents(schema)))
        if not self._by_name:
            raise FileNotFoundError(f"no schemas found in {proto_dir}")
        self._registry = Registry().with_resources(resources)
        self._validators: dict[str, Draft202012Validator] = {}

    @property
    def names(self) -> list[str]:
        return sorted(self._by_name)

    def validator(self, name: str) -> Draft202012Validator:
        if name not in self._validators:
            self._validators[name] = Draft202012Validator(
                self._by_name[name], registry=self._registry, format_checker=FormatChecker()
            )
        return self._validators[name]

    def validate(self, name: str, instance: Any) -> None:
        """Raise ProtocolError if ``instance`` does not match schema ``name``."""
        try:
            self.validator(name).validate(instance)
        except ValidationError as exc:
            where = "/".join(str(p) for p in exc.absolute_path) or "<root>"
            raise ProtocolError(f"{name}: {where}: {exc.message}") from exc

    def validate_message(self, message: Any) -> None:
        """Validate a node <-> hub message against the schema of its ``type``."""
        msg_type = message.get("type") if isinstance(message, dict) else None
        if msg_type not in MESSAGE_TYPES:
            raise ProtocolError(f"unknown message type: {msg_type!r}")
        self.validate(msg_type, message)


@lru_cache
def load_schemas(proto_dir: Path) -> SchemaSet:
    return SchemaSet(proto_dir)


def load_schema_sets(proto_root: Path) -> dict[int, SchemaSet]:
    """Schemas of every supported protocol version: ``<proto_root>/v<N>``."""
    return {v: load_schemas(proto_root / f"v{v}") for v in sorted(SUPPORTED_VERSIONS)}


def now_rfc3339() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def envelope(
    msg_type: str, body: dict[str, Any], re: str | None = None, v: int = PROTOCOL_VERSION
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "v": v,
        "type": msg_type,
        "id": str(uuid.uuid4()),
        "ts": now_rfc3339(),
        "body": body,
    }
    if re is not None:
        msg["re"] = re
    return msg


def canonical_json(obj: Any) -> str:
    """Canonical form used for config hashes (node computes the same)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# Actions a freshly paired node may run (E1-02: real llamaserver runner).
DEFAULT_ACTIONS = [
    "models.list",
    "models.load",
    "models.unload",
    "models.download",
    "models.delete",
    "node.benchmark",
    "models.proxy",
    "node.update",
    "models.send",
    "models.receive",
]
DEFAULT_IDLE_TTL_SECONDS = 1800
DEFAULT_WATCHER_COOLDOWN_SECONDS = 60


def default_pressure(enabled: bool = True) -> dict[str, Any]:
    """Defaults for ``config.pressure`` (E1-06, docs/PROTOCOL.md).

    ``enabled`` is GPU VRAM pressure handling; nodes only watch discrete GPUs, so it is
    harmless on nodes without one. ``ram.enabled`` is left out on purpose: the node decides
    (on for nodes without a dedicated GPU, off with one; E2-05, D27); an admin who sets it
    overrides that.
    """
    return {
        "enabled": enabled,
        "poll_seconds": 2,
        "sustain_seconds": 3,
        "unload_below_free_mb": 1024,
        "margin_mb": 1024,
        "reload_after_seconds": 60,
        "reload_headroom_mb": 2048,
        "reload_max_foreign_gpu_util": 30,
        "min_off_seconds": 120,
        "reload_while_rule_matches": False,
        "foreign_shared_trigger_mb": 1024,
        "ram": {
            "unload_below_free_mb": 2048,
            "margin_mb": 1024,
            "reload_headroom_mb": 1024,
        },
    }


DEFAULT_RESIDENT_PRIORITY = 50


# What a tool node (role tools, E2-04) runs for: the read-only capabilities. All of them are
# switched on by default — who may use them is the user's grant (D20), and every call is audited.
TOOL_CAPABILITIES = [
    "system.info",
    "system.memory",
    "disk.usage",
    "docker.ps",
    "docker.inspect",
    "docker.logs",
    "compose.projects",
    "compose.file",
    "compose.logs",
    "journal.logs",
    "service.status",
    "service.failed",
    # knowledge base files (E2-03): offered only where tools.docs_roots releases a directory
    "docs.list",
    "docs.read",
    # connectors (E2-04c): offered by a node only where its connectors file has the section
    "kuma.monitors",
    "omada.sites",
    "omada.devices",
    "omada.clients",
    "omada.alerts",
    "omada.events",
    "omada.audit_logs",
    "omada.firmware",
    # identity (E3-12): offered by a node only where its connectors file has the keycloak section
    "keycloak.snapshot",
    "keycloak.user",
    # web (E3-04): offered by a node only where tools.searxng_url / tools.web_fetch are set
    "web.search",
    "web.fetch",
    # pictures for a report (2026-11-13): with searxng_url / web_fetch, like the two above
    "web.images",
    "web.image",
    # collecting capabilities (E3-04): one call instead of six; the last two are offered by a node
    # only where tools.cert_targets / tools.net_targets / tools.dns_names are configured
    "health.check",
    "logs.search",
    "certs.expiry",
    "net.check",
    # what runs on this machine, for the hub's inventory (E3-10)
    "inventory.snapshot",
]


# What a dev node (role dev, E6-01 S1, D52) offers: the workshop capabilities. repo.test only
# appears where the node's config names a runner; the node leaves out what it cannot run.
DEV_CAPABILITIES = [
    "repo.sync",
    "repo.status",
    "repo.list",
    "repo.read",
    "repo.search",
    # the map of a folder: files with their named places and lines (plan step 1.11)
    "repo.outline",
    "repo.diff",
    "repo.start",
    "repo.edit",
    "repo.write",
    "repo.apply_patch",
    "repo.reset",
    "repo.test",
    # a test run that goes on in the node after the call (and is asked for, stopped, waited for)
    "repo.test_start",
    "repo.test_status",
    "repo.test_cancel",
    "repo.build_node",
    "repo.build_list",
    "repo.push",
    # D76: local repositories — the branches, keeping work on a work branch, and the person's own
    # steps from the hub's page (run by workshop.manage only, never by an assistant)
    "repo.branches",
    "repo.commit",
    "workshop.create",
    "workshop.clone",
    "workshop.branch",
    "workshop.merge",
    "workshop.publish",
    "workshop.pull",
    "workshop.remove",
]


def default_dev_config(heartbeat_interval: int) -> dict[str, Any]:
    """Desired config of a node paired with ``--role dev``: no runner, no GPU work, only the
    workshop capabilities through ``capability.invoke`` (the ones that change just the scratch
    worktree run without a click; a push is a separate, confirmed step). ``tools.workshop`` is
    left to the maintainer: the clone directory, allowed hosts and the test runner are the
    node's business."""
    config = default_tools_config(heartbeat_interval)
    config["roles"] = ["dev"]
    # capability.execute only for repo.push, the one step that leaves the workshop: it runs for a
    # confirmed proposal and for nothing else (the node checks proposal id and hash)
    config["allowed_actions"] = ["capability.invoke", "capability.execute", "workshop.manage"]
    config["capabilities"] = list(DEV_CAPABILITIES)
    return config


BUILD_CAPABILITIES = [
    "build.fetch",
    "build.node_release",
    "build.hub_image",
    "build.node_container_image",
    "build.smoke_test",
]


def default_build_config(heartbeat_interval: int) -> dict[str, Any]:
    """Desired config of a node paired with ``--role build`` (NS-01): the build service only.
    Nothing here can read or change the machine or the repository except through the named
    ``build.*`` steps, which the hub runs on the admin's click and no assistant can."""
    config = default_tools_config(heartbeat_interval)
    config["roles"] = ["build"]
    config["allowed_actions"] = ["build.manage"]
    config["capabilities"] = list(BUILD_CAPABILITIES)
    return config


def default_tools_config(heartbeat_interval: int) -> dict[str, Any]:
    """Desired config of a node paired with ``--role tools``: no runner, no GPU work, no yielding
    to a person (there is none), the read-only capabilities and ``capability.invoke``."""
    return {
        "roles": ["tools"],
        "runner": {"type": "none"},
        "allowed_actions": ["capability.invoke"],
        "heartbeat_interval_seconds": heartbeat_interval,
        "idle_ttl_seconds": 0,
        "watcher": {"grace_seconds": 5, "cooldown_seconds": 60, "rules": []},
        "capabilities": list(TOOL_CAPABILITIES),
        "residents": [],
        "pressure": default_pressure(enabled=False),
        "yield": {"enabled": False},
        "power": {"profile": "balanced"},
        "tools": {},
    }


def default_node_config(os_name: str, heartbeat_interval: int) -> dict[str, Any]:
    """Desired config for a freshly paired node (D9: node-managed llama-server on every OS).

    ``os_name`` is kept for future per-OS defaults; the llama.cpp backend is ``auto`` and
    resolved by the node from its detected hardware. Admins edit the stored config via
    ``PUT /api/nodes/{id}/config``.
    """
    return {
        "roles": ["gpu"],
        "runner": {"type": "llamaserver", "model_dirs": [], "backend": "auto"},
        "allowed_actions": list(DEFAULT_ACTIONS),
        "heartbeat_interval_seconds": heartbeat_interval,
        "idle_ttl_seconds": DEFAULT_IDLE_TTL_SECONDS,
        "watcher": {
            "grace_seconds": 5,
            "cooldown_seconds": DEFAULT_WATCHER_COOLDOWN_SECONDS,
            "rules": [],
        },
        "capabilities": [],
        "residents": [],
        "pressure": default_pressure(),
    }
