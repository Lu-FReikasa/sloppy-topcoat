"""An external repository with a local clone is read in the clone (live 2026-11-20: a Development
check asked for the external repository, got the node's old copy of GitHub and concluded that a
file of the clone did not exist)."""

from fastapi.testclient import TestClient

from tests.test_local_repos import AUTH, dev_node, external  # noqa: F401
from tests.test_tools import run


def test_a_call_on_an_external_repository_goes_to_its_local_clone(
    client: TestClient,
    connect,
    external: str,  # noqa: F811
) -> None:
    node_id, node = dev_node(client, connect)
    r = client.post(f"/api/dev/repos/{external}/clone", headers=AUTH, json={"name": "app"})
    assert r.status_code == 201, r.text
    r = run(client, node_id, "repo.sync", {"repo": "upstream-app"})
    assert r.status_code == 200, r.text
    call = node.capability_calls[-1]
    assert call["repo"]["local"] is True and call["repo"]["url"] == ""
    assert call["input"]["repo"] == "app"


def test_an_external_repository_without_a_clone_is_still_read_from_github(
    client: TestClient,
    connect,
    external: str,  # noqa: F811
) -> None:
    node_id, node = dev_node(client, connect)
    r = run(client, node_id, "repo.sync", {"repo": "upstream-app"})
    assert r.status_code == 200, r.text
    assert node.capability_calls[-1]["repo"]["url"] == "https://github.com/acme/app.git"
