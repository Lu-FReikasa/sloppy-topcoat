import hashlib
import json
from pathlib import Path

import pytest

from topcoat_hub.config import REPO_ROOT
from topcoat_hub.protocol import (
    MESSAGE_TYPES,
    PROTOCOL_VERSION,
    ProtocolError,
    canonical_json,
    config_hash,
    default_node_config,
    envelope,
    load_schemas,
)

# v1 is current; v0 (the pre-freeze draft) is still accepted from nodes for one more release.
VERSIONS = (0, 1)
ALL_SCHEMAS = {v: load_schemas(REPO_ROOT / "proto" / f"v{v}") for v in VERSIONS}
SCHEMAS = ALL_SCHEMAS[PROTOCOL_VERSION]
EXAMPLES = REPO_ROOT / "proto" / "examples" / f"v{PROTOCOL_VERSION}"
CASES = [
    pytest.param(v, p, id=f"v{v}/{p.name}")
    for v in VERSIONS
    for p in sorted((REPO_ROOT / "proto" / "examples" / f"v{v}" / "valid").glob("*.json"))
]
INVALID_CASES = [
    pytest.param(v, p, id=f"v{v}/{p.name}")
    for v in VERSIONS
    for p in sorted((REPO_ROOT / "proto" / "examples" / f"v{v}" / "invalid").glob("*.json"))
]


def _schema_for(path: Path) -> str:
    return path.name.split(".")[0]


@pytest.mark.parametrize(("version", "path"), CASES)
def test_valid_examples(version: int, path: Path) -> None:
    schemas = ALL_SCHEMAS[version]
    instance = json.loads(path.read_text())
    schemas.validate(_schema_for(path), instance)
    if _schema_for(path) in MESSAGE_TYPES:
        schemas.validate("message", instance)
        schemas.validate_message(instance)
        assert instance["v"] == version


@pytest.mark.parametrize(("version", "path"), INVALID_CASES)
def test_invalid_examples(version: int, path: Path) -> None:
    instance = json.loads(path.read_text())
    with pytest.raises(ProtocolError):
        ALL_SCHEMAS[version].validate(_schema_for(path), instance)


def test_versions_do_not_accept_each_others_messages() -> None:
    v1 = json.loads((EXAMPLES / "valid" / "action.load.json").read_text())
    ALL_SCHEMAS[1].validate_message(v1)
    with pytest.raises(ProtocolError):
        ALL_SCHEMAS[0].validate_message(v1)
    v0 = {**v1, "v": 0}
    ALL_SCHEMAS[0].validate_message(v0)
    with pytest.raises(ProtocolError):
        ALL_SCHEMAS[1].validate_message(v0)


def test_every_message_type_has_a_valid_example() -> None:
    covered = {_schema_for(p) for p in (EXAMPLES / "valid").glob("*.json")}
    assert set(MESSAGE_TYPES) <= covered
    assert {"pair_request", "pair_response"} <= covered


def test_envelope_builds_valid_messages() -> None:
    SCHEMAS.validate_message(envelope("error", {"code": "invalid_message", "message": "x"}))
    cfg = default_node_config("linux", 10)
    SCHEMAS.validate_message(envelope("config", {"config_hash": config_hash(cfg), "config": cfg}))
    SCHEMAS.validate_message(envelope("action", {"name": "models.load", "args": {"model": "m"}}))


@pytest.mark.parametrize("os_name", ["windows", "linux"])
def test_default_config_is_valid(os_name: str) -> None:
    cfg = default_node_config(os_name, 10)
    SCHEMAS.validate("node_config", cfg)
    assert cfg["runner"]["type"] == "llamaserver"
    assert cfg["runner"]["backend"] == "auto"
    assert cfg["allowed_actions"] == [
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
    assert cfg["idle_ttl_seconds"] == 1800
    assert cfg["watcher"] == {"grace_seconds": 5, "cooldown_seconds": 60, "rules": []}
    assert cfg["residents"] == []
    assert cfg["pressure"]["enabled"] is True
    assert cfg["pressure"]["unload_below_free_mb"] == 1024
    assert cfg["pressure"]["foreign_shared_trigger_mb"] == 1024
    assert "enabled" not in cfg["pressure"]["ram"]  # the node decides by its hardware (D27)


def test_canonical_json_matches_node_implementation() -> None:
    # The same vector is asserted in the Go node tests.
    obj = {"b": 1, "a": [1, 2], "c": {"y": "x", "x": True}}
    assert canonical_json(obj) == '{"a":[1,2],"b":1,"c":{"x":true,"y":"x"}}'
    assert config_hash(obj) == hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def test_unknown_type_rejected() -> None:
    with pytest.raises(ProtocolError, match="unknown message type"):
        SCHEMAS.validate_message({"type": "shell"})
