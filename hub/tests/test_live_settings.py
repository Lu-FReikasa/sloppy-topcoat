"""Parameters an admin changes while the hub runs (E7-03, D58): an allowlist with types and bounds,
a change that reaches the running hub, a history, a reset, a preset, and nothing that is secret."""

import asyncio
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub.config import Settings
from topcoat_hub.live_settings import BY_KEY, PARAMS, LiveSettings, SettingError

SECRETS = (
    "database_url",
    "dev_admin_token",
    "github_token",
    "secret_dir",
    "tls_dir",
    "tls_key_file",
    "host",
    "port",
)


def fresh() -> LiveSettings:
    return LiveSettings(Settings(dev_admin_token="x"))


# --- the allowlist ------------------------------------------------------------------------------


def test_every_listed_parameter_is_a_real_setting_with_a_value_it_accepts_itself() -> None:
    live = fresh()
    for p in PARAMS:
        assert p.key in Settings.model_fields, p.key
        live.check(p.key, live.defaults[p.key])  # the default is always acceptable
        if p.kind in ("int", "float") and p.low is not None and p.high is not None:
            assert p.low <= live.defaults[p.key] <= p.high, p.key  # and inside its own bounds


def test_nothing_secret_or_wired_to_the_machine_can_be_reached() -> None:
    live = fresh()
    for key in SECRETS:
        assert key not in BY_KEY
        with pytest.raises(SettingError) as err:
            live.check(key, "x")
        assert err.value.code == "unknown"
    listed = {p.key for p in PARAMS}
    secretish = re.compile(r"(_token|_key|_secret|_password|_url|_file|_dir|_path)$")
    assert not [k for k in listed if secretish.search(k)]  # by name, too


def test_values_are_checked_by_type_and_by_bounds() -> None:
    live = fresh()
    assert live.check("agent_max_turns", 12) == 12
    assert live.check("agent_max_turns", 12.0) == 12
    assert live.check("agent_time_budget_seconds", 90) == 90.0  # a whole number is a fine float
    for bad in ("12", None, True, 12.5, [1]):
        with pytest.raises(SettingError) as err:
            live.check("agent_max_turns", bad)
        assert err.value.code == "invalid", bad
    for out in (0, 201, -3):
        with pytest.raises(SettingError) as err:
            live.check("agent_max_turns", out)
        assert err.value.code == "range"
    with pytest.raises(SettingError):
        live.check("agent_tool_mode", "magic")
    assert live.check("agent_tool_mode", "json") == "json"
    with pytest.raises(SettingError):
        live.check("review_enabled", 1)  # a switch is true or false, nothing else
    assert live.check("review_enabled", False) is False
    assert live.check("chat_sampling", {"qwen": {"temperature": 0.4}}) == {
        "qwen": {"temperature": 0.4}
    }
    with pytest.raises(SettingError):
        live.check("chat_sampling", {"qwen": "hot"})


# --- changing, resetting, hooks ----------------------------------------------------------------


def test_a_change_reaches_the_running_settings_and_a_reset_brings_the_default_back() -> None:
    live = fresh()
    default = live.settings.agent_max_turns
    asyncio.run(live.set("agent_max_turns", 7, None))
    assert live.settings.agent_max_turns == 7 and live.overrides == {"agent_max_turns": 7}
    view = {p["key"]: p for p in live.view()["params"]}["agent_max_turns"]
    assert (view["value"], view["default"], view["overridden"]) == (7, default, True)
    assert live.export() == {"version": 1, "values": {"agent_max_turns": 7}}
    asyncio.run(live.reset("agent_max_turns", None))
    assert live.settings.agent_max_turns == default and live.overrides == {}


def test_a_group_hook_runs_when_a_value_of_the_group_changes() -> None:
    live = fresh()
    seen: list[int] = []
    live.hooks["agent"] = lambda s: seen.append(s.agent_max_turns)
    asyncio.run(live.set("agent_max_turns", 9, None))
    asyncio.run(live.set("review_hours", 5, None))  # another group: no call
    asyncio.run(live.reset("agent_max_turns", None))
    assert seen == [9, Settings(dev_admin_token="x").agent_max_turns]


def test_a_hook_that_fails_does_not_undo_the_change() -> None:
    live = fresh()

    def boom(_: Settings) -> None:
        raise RuntimeError("hook")

    live.hooks["agent"] = boom
    asyncio.run(live.set("agent_max_turns", 5, None))
    assert live.settings.agent_max_turns == 5


# --- through the API, with the database ----------------------------------------------------------


def params(client: TestClient) -> dict[str, dict[str, Any]]:
    r = client.get("/api/admin/params", headers=AUTH)
    assert r.status_code == 200, r.text
    return {p["key"]: p for p in r.json()["params"]}


def test_the_admin_lists_changes_and_resets_a_parameter(client: TestClient) -> None:
    listed = params(client)
    assert listed["agent_max_turns"]["overridden"] is False
    default = listed["agent_max_turns"]["default"]
    assert "dev_admin_token" not in listed

    r = client.put("/api/admin/params/agent_max_turns", headers=AUTH, json={"value": 6})
    assert r.status_code == 200 and r.json() == {
        "key": "agent_max_turns",
        "value": 6,
        "overridden": True,
    }
    # the running hub uses it: the setting and the limits of the next check
    assert client.app.state.settings.agent_max_turns == 6
    assert client.app.state.agents.loop.limits.max_turns == 6
    assert params(client)["agent_max_turns"]["value"] == 6

    client.put("/api/admin/params/agent_max_turns", headers=AUTH, json={"value": 8})
    history = client.get("/api/admin/params/agent_max_turns/history", headers=AUTH).json()
    assert [(h["old"], h["new"]) for h in history] == [(6, 8), (default, 6)]
    assert history[0]["by"] and history[0]["at"]

    r = client.delete("/api/admin/params/agent_max_turns", headers=AUTH)
    assert r.json() == {"key": "agent_max_turns", "value": default, "overridden": False}
    assert client.app.state.agents.loop.limits.max_turns == default
    history = client.get("/api/admin/params/agent_max_turns/history", headers=AUTH).json()
    assert history[0]["reset"] is True and history[0]["old"] == 8


def test_a_value_that_does_not_fit_and_a_name_that_is_not_listed_are_refused(
    client: TestClient,
) -> None:
    r = client.put("/api/admin/params/agent_max_turns", headers=AUTH, json={"value": 5000})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "range"
    r = client.put("/api/admin/params/agent_max_turns", headers=AUTH, json={"value": "many"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid"
    for name in ("dev_admin_token", "database_url", "nothing_like_it"):
        r = client.put(f"/api/admin/params/{name}", headers=AUTH, json={"value": "x"})
        assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown"
        assert client.delete(f"/api/admin/params/{name}", headers=AUTH).status_code == 404
        assert client.get(f"/api/admin/params/{name}/history", headers=AUTH).status_code == 404
    assert client.app.state.settings.dev_admin_token  # nothing of it changed


def test_only_an_admin_can_read_or_change_parameters(client: TestClient) -> None:
    _, bob = make_user(TEST_DB_URL, "bob")
    assert client.get("/api/admin/params", headers=bob).status_code == 403
    assert (
        client.put("/api/admin/params/agent_max_turns", headers=bob, json={"value": 3}).status_code
        == 403
    )
    assert client.delete("/api/admin/params/agent_max_turns", headers=bob).status_code == 403
    assert client.get("/api/admin/params").status_code in (401, 403)


def test_a_preset_is_exported_and_put_back_all_or_nothing(client: TestClient) -> None:
    client.put("/api/admin/params/agent_max_turns", headers=AUTH, json={"value": 11})
    client.put("/api/admin/params/review_batch", headers=AUTH, json={"value": 4})
    preset = client.get("/api/admin/params/export", headers=AUTH).json()
    assert preset == {"version": 1, "values": {"agent_max_turns": 11, "review_batch": 4}}
    client.delete("/api/admin/params/agent_max_turns", headers=AUTH)
    client.delete("/api/admin/params/review_batch", headers=AUTH)

    r = client.put("/api/admin/params/import", headers=AUTH, json=preset)
    assert r.json() == {"applied": ["agent_max_turns", "review_batch"]}
    assert client.app.state.settings.agent_max_turns == 11
    assert client.app.state.settings.review_batch == 4

    # one bad value: nothing of the preset is applied
    bad = {"version": 1, "values": {"agent_max_turns": 13, "review_batch": 0}}
    assert client.put("/api/admin/params/import", headers=AUTH, json=bad).status_code == 422
    assert client.app.state.settings.agent_max_turns == 11
    secret = {"version": 1, "values": {"dev_admin_token": "stolen"}}
    assert client.put("/api/admin/params/import", headers=AUTH, json=secret).status_code == 404


def test_the_values_survive_a_restart_and_a_value_that_no_longer_fits_is_skipped(
    client: TestClient,
) -> None:
    client.put("/api/admin/params/agent_max_turns", headers=AUTH, json={"value": 14})
    client.put("/api/admin/params/review_batch", headers=AUTH, json={"value": 6})
    # a value written by an older build that the bounds now refuse, and one for a name that is gone
    from sqlalchemy import create_engine, text

    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hub_settings (key, value) VALUES ('live.review_batch', '9999')"
                " ON CONFLICT (key) DO UPDATE SET value = '9999'"
            )
        )
        conn.execute(
            text("INSERT INTO hub_settings (key, value) VALUES ('live.removed_name', '1')")
        )
    engine.dispose()

    restarted = LiveSettings(Settings(dev_admin_token="x"), client.app.state.sessionmaker)
    applied = client.portal.call(restarted.load)
    assert applied == 1
    assert restarted.settings.agent_max_turns == 14
    assert restarted.settings.review_batch == restarted.defaults["review_batch"]  # skipped
    assert "removed_name" not in restarted.overrides
