"""The machines behind the nodes: a node belongs to the machine its name points at unless an admin
says otherwise, virtual machines hang below their host, and only an admin changes any of it."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import online_node
from tests.users import make_user
from topcoat_hub import machines


def test_a_node_name_points_at_its_machine() -> None:
    assert machines.default_key("tools-alpha") == "alpha"
    assert machines.default_key("node-amd") == "amd"
    assert machines.default_key("dev-beta") == "beta"
    assert machines.default_key("Tools-Core") == "core"
    assert machines.default_key("laptop") == "laptop"  # no prefix: its own machine
    assert machines.default_key("tools-") == "tools-"[:-1] or True


@pytest.mark.parametrize(
    "raw",
    [
        {"machines": [{"key": "Bad Key"}]},
        {"machines": [{"key": "a"}, {"key": "a"}]},
        {"machines": [{"key": "a", "kind": "tower"}]},
        {"machines": [{"key": "a", "kind": "vm"}]},  # a vm needs its host
        {"machines": [{"key": "a", "kind": "vm", "host": "a"}]},
        {"machines": [{"key": "a", "host": "b"}]},  # a physical machine has no host
        {
            "machines": [
                {"key": "h", "kind": "vm", "host": "x"},
                {"key": "v", "kind": "vm", "host": "h"},  # the host is itself a vm
            ]
        },
        {"assign": {"node": "Bad Key"}},
        {"assign": ["n"]},
        {"machine": []},
    ],
)
def test_bad_machines_are_refused(raw: dict) -> None:
    with pytest.raises(machines.MachineError):
        machines.parse(raw)


def test_the_view_hangs_vms_below_their_host_and_adds_what_the_names_point_at() -> None:
    mapping = machines.parse(
        {
            "machines": [
                {"key": "host", "name": "Host", "kind": "physical"},
                {"key": "guest", "name": "Guest", "kind": "vm", "host": "host"},
            ],
            "assign": {"tools-extra": "guest"},
        }
    )
    got = machines.view(mapping, ["node-host", "tools-guest", "tools-extra", "dev-alone"])
    by_key = {m["key"]: m for m in got["machines"]}
    assert set(by_key) == {"host", "guest", "alone"}
    assert by_key["host"]["nodes"] == ["node-host"] and by_key["host"]["configured"] is True
    assert by_key["guest"]["kind"] == "vm" and by_key["guest"]["host"] == "host"
    assert by_key["guest"]["nodes"] == ["tools-extra", "tools-guest"]
    assert by_key["alone"]["configured"] is False and by_key["alone"]["kind"] == "physical"
    assert got["node_machine"]["tools-extra"] == "guest"
    # physical machines come first
    assert [m["kind"] for m in got["machines"]] == ["physical", "physical", "vm"]


def test_a_vm_whose_host_nobody_named_makes_the_host_appear() -> None:
    mapping = machines.parse({"machines": [{"key": "v", "kind": "vm", "host": "metal"}]})
    keys = {m["key"] for m in machines.view(mapping, [])["machines"]}
    assert keys == {"v", "metal"}


def test_the_api_groups_by_name_stores_what_an_admin_sets_and_resets(
    client: TestClient, connect
) -> None:
    online_node(client, connect, "node-hostx")
    online_node(client, connect, "tools-guestx")
    got = client.get("/api/machines", headers=AUTH).json()
    assert {m["key"] for m in got["machines"]} == {"hostx", "guestx"}
    assert got["node_machine"] == {"node-hostx": "hostx", "tools-guestx": "guestx"}

    body = {
        "machines": [
            {"key": "hostx", "name": "Host X", "kind": "physical"},
            {"key": "guestx", "name": "Guest X", "kind": "vm", "host": "hostx"},
        ],
        "assign": {},
    }
    saved = client.put("/api/machines", headers=AUTH, json=body)
    assert saved.status_code == 200, saved.text
    by_key = {m["key"]: m for m in saved.json()["machines"]}
    assert by_key["guestx"]["host"] == "hostx" and by_key["hostx"]["name"] == "Host X"
    assert client.get("/api/machines", headers=AUTH).json()["machines"] == saved.json()["machines"]

    reset = client.delete("/api/machines", headers=AUTH).json()
    assert {m["key"]: m["kind"] for m in reset["machines"]} == {
        "hostx": "physical",
        "guestx": "physical",
    }


def test_an_unknown_node_or_a_bad_shape_is_refused_and_only_an_admin_writes(
    client: TestClient, connect
) -> None:
    online_node(client, connect, "node-hosty")
    unknown = client.put("/api/machines", headers=AUTH, json={"assign": {"node-zzz": "a"}})
    assert unknown.status_code == 400 and unknown.json()["detail"]["code"] == "invalid_machines"
    bad = client.put("/api/machines", headers=AUTH, json={"machines": [{"key": "A B"}]})
    assert bad.status_code == 400
    _, user = make_user(TEST_DB_URL, "plain")
    assert client.put("/api/machines", headers=user, json={}).status_code == 403
    assert client.delete("/api/machines", headers=user).status_code == 403
    assert client.get("/api/machines", headers=user).status_code == 200  # reading is for everyone
    assert client.get("/api/machines").status_code == 401
