"""``proto/capabilities.json`` is the one source of capability names and default roles (#206)."""

import json
from pathlib import Path

import pytest

from topcoat_hub import protocol


def _write(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps({"capabilities": entries}), encoding="utf-8")
    return path


def test_the_role_lists_come_from_the_file() -> None:
    by_name = {c.name: c for c in protocol.CAPABILITIES}
    assert protocol.TOOL_CAPABILITIES[:2] == ["system.info", "system.memory"]
    for role, names in (
        ("tools", protocol.TOOL_CAPABILITIES),
        ("dev", protocol.DEV_CAPABILITIES),
        ("build", protocol.BUILD_CAPABILITIES),
    ):
        assert names and all(role in by_name[n].roles for n in names)
    # what no role gets by default is still listed: an admin switches it on by hand
    assert by_name["windows.system"].roles == () and by_name["hub.update"].roles == ()
    assert protocol.default_dev_config(30)["capabilities"] == protocol.DEV_CAPABILITIES


@pytest.mark.parametrize(
    ("entries", "reason"),
    [
        ([{"name": "a.b", "group": "x", "roles": []}] * 2, "listed twice"),
        ([{"name": "a.b", "group": "x", "roles": ["admin"]}], "unknown or repeated role"),
        ([{"name": "a.b", "group": "x", "roles": ["dev", "dev"]}], "unknown or repeated role"),
        ([{"name": "ab", "group": "x", "roles": []}], "not a capability name"),
        ([{"name": "a.b", "group": "", "roles": []}], "has no group"),
    ],
)
def test_a_malformed_file_is_refused(tmp_path: Path, entries: list[dict], reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        protocol.load_capabilities(_write(tmp_path, entries))
