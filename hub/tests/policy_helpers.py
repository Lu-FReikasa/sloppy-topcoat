"""Tests state a scheduling policy in the shape of the older lists (``roles``, ``no_chat`` ...),
which is how the production hub's policy was written, and the helper turns it into the table of
applications the hub reads now (``legacy_policy.convert``, the step the hub takes at start)."""

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from topcoat_hub import legacy_policy

NODES = ["node-amd", "node-laptop"]


def table_of(legacy: dict[str, Any], nodes: list[str] | None = None) -> dict[str, Any]:
    policy, _, _ = legacy_policy.convert(legacy, {}, nodes or NODES)
    return {key: row.to_json() for key, row in policy.applications.items()}


def apply_legacy(client: TestClient, legacy: dict[str, Any], nodes: list[str] | None = None) -> Any:
    """Store the table that the older policy ``legacy`` stands for."""
    r = client.put(
        "/api/scheduling/applications",
        headers=AUTH,
        json={"rows": table_of(legacy, nodes)},
    )
    assert r.status_code == 200, r.text
    return r.json()


def use_model_everywhere(client: TestClient, model: str) -> None:
    """Every application and the system rows (but the gateway's own) use ``model``: what the global
    default model did before there was none (models are released per node and chosen per
    application)."""
    keys = client.get("/api/scheduling/applications", headers=AUTH).json()["keys"]
    rows = {key: {"models": [model]} for key in keys if key != "api"}
    r = client.put("/api/scheduling/applications", headers=AUTH, json={"rows": rows})
    assert r.status_code == 200, r.text
