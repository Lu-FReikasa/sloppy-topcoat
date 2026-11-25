import types

from topcoat_hub.topcoat_self import _machine_row


def test_short_form_lists_the_models() -> None:
    node = types.SimpleNamespace(name="node-amd", os="windows")
    view = {
        "state": "available",
        "last_seen": None,
        "cpu_util": 0.1,
        "ram_total_mb": None,
        "ram_used_mb": None,
        "gpus": [],
        "loaded": [],
        "loading": [],
        "models": [
            {"name": "gpt-oss-20b"},
            {"name": "Qwen3.5-9B"},
        ],
        "roles": [],
        "cpu": 0,
    }
    row = _machine_row(node, view, tools=0, full=False)
    assert "models" in row
    assert row["models"] == ["gpt-oss-20b", "Qwen3.5-9B"]


def test_full_form_lists_the_models() -> None:
    node = types.SimpleNamespace(name="node-amd", os="windows")
    view = {
        "state": "available",
        "last_seen": None,
        "cpu_util": 0.1,
        "ram_total_mb": None,
        "ram_used_mb": None,
        "gpus": [],
        "loaded": [],
        "loading": [],
        "models": [
            {"name": "gpt-oss-20b"},
            {"name": "Qwen3.5-9B"},
        ],
        "roles": [],
        "cpu": 0,
    }
    row = _machine_row(node, view, tools=0, full=True)
    assert "models" in row
    assert row["models"] == ["gpt-oss-20b", "Qwen3.5-9B"]
