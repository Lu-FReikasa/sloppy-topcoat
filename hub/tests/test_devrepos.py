"""Registered repositories (E6-01 S0, D52): the token is sealed and never returned, the tool picks
the repository by name, nothing but ``GET`` ever goes to GitHub. The first group needs no database;
the API group does (like the other API tests)."""

import asyncio
import stat
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH
from tests.test_github_access import ADMIN, TOKEN, FakeGitHub
from tests.users import make_user
from topcoat_hub import github_access as gh
from topcoat_hub.config import Settings
from topcoat_hub.devrepos import RepoError, check_fields, parse_repo
from topcoat_hub.main import create_app
from topcoat_hub.secretbox import SecretBox, SecretError
from topcoat_hub.tools import ToolError

OTHER_TOKEN = "github_pat_" + "B" * 40


# --- no database ---------------------------------------------------------------------------------


def test_the_secret_box_seals_and_keeps_its_key_private(tmp_path: Path) -> None:
    box = SecretBox(tmp_path / "keys")
    sealed = box.encrypt("hunter2-hunter2")
    assert "hunter2" not in sealed
    assert box.decrypt(sealed) == "hunter2-hunter2"
    key = tmp_path / "keys" / "secrets.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    # a second box on the same directory reads what the first sealed
    assert SecretBox(tmp_path / "keys").decrypt(sealed) == "hunter2-hunter2"
    # another key cannot read it, and says so instead of returning garbage
    with pytest.raises(SecretError):
        SecretBox(tmp_path / "other").decrypt(sealed)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("owner/name", "owner/name"),
        ("https://github.com/owner/name", "owner/name"),
        ("https://github.com/owner/name.git", "owner/name"),
        ("https://github.com/owner/name/", "owner/name"),
        ("git@github.com:owner/name.git", "owner/name"),
        ("  owner/na.me-1  ", "owner/na.me-1"),
    ],
)
def test_a_repository_is_given_as_a_path_or_a_github_address(given: str, expected: str) -> None:
    assert parse_repo(given) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "owner", "a/b/c", "https://evil.example/owner/name", "owner/name; rm -rf /", "../x/y"],
)
def test_anything_else_is_refused(bad: str) -> None:
    with pytest.raises(RepoError) as e:
        parse_repo(bad)
    assert e.value.status == 422


def test_mode_template_and_name_are_checked() -> None:
    check_fields("topcoat", "read", "make-check")
    for args in (("", "read", "none"), ("x", "write", "none"), ("x", "read", "rm -rf /")):
        with pytest.raises(RepoError):
            check_fields(*args)


class _Reader:
    """A token that may read the repository and its branches but not its pull requests."""

    repo = "acme/app"

    def __init__(self, prs: Any) -> None:
        self._prs = prs

    async def get(self, path: str, params: Any = None) -> Any:
        return {"default_branch": "main", "private": True, "permissions": {"push": True}}

    async def branches(self, page: int) -> Any:
        return [{"name": "main"}, {"name": "topcoat/x"}]

    async def prs(self, state: str, page: int) -> Any:
        if isinstance(self._prs, Exception):
            raise self._prs
        return self._prs


def test_a_token_without_the_pull_request_right_still_registers() -> None:
    from topcoat_hub.devrepos import read_state

    forbidden = gh.GitHubError(502, "forbidden_or_limit", "GitHub refused")
    state = asyncio.run(read_state(_Reader(forbidden)))  # type: ignore[arg-type]
    assert state["open_prs"] is None and state["can_push"] is True
    assert state["test_branches"] == ["topcoat/x"]
    # any other failure on the pull requests is still a failure
    with pytest.raises(gh.GitHubError):
        asyncio.run(read_state(_Reader(gh.GitHubError(502, "token_rejected", "no"))))  # type: ignore[arg-type]
    assert asyncio.run(read_state(_Reader([{}, {}])))["open_prs"] == 2  # type: ignore[arg-type]


class FakeStore:
    """What GitHubService needs of the store, without a database."""

    def __init__(self, repos: dict[str, gh.GitHubReader]) -> None:
        self.repos = repos

    async def list(self, user_id: uuid.UUID) -> list[Any]:
        return [type("Row", (), {"name": n})() for n in self.repos]

    def reader(self, row: Any) -> gh.GitHubReader:
        return self.repos[row.name]


class TwoRepos(FakeGitHub):
    def route(self, path: str, query: str) -> tuple[int, Any]:
        if path == "/repos/acme/one/branches":
            return 200, [{"name": "main", "commit": {"sha": "a" * 40}}]
        if path == "/repos/acme/two/branches":
            return 200, [{"name": "dev", "commit": {"sha": "b" * 40}}]
        return 404, {"message": "Not Found"}


@pytest.fixture
def two() -> Iterator[TwoRepos]:
    f = TwoRepos()
    yield f
    f.server.shutdown()


def tool(fake: TwoRepos, names: list[str]) -> gh.GitHubService:
    settings = Settings(github_api_url=fake.url)  # no repository in the hub's own settings
    readers = {
        n: gh.GitHubReader.for_repo(settings, TOKEN if n == "one" else OTHER_TOKEN, f"acme/{n}")
        for n in names
    }
    return gh.GitHubService(settings, FakeStore(readers))


def call(svc: gh.GitHubService, **inp: Any) -> dict[str, Any]:
    return asyncio.run(svc.run_tool(ADMIN, gh.CAPABILITY, inp))["output"]


def test_a_registered_repository_alone_is_enough_and_needs_no_name(two: TwoRepos) -> None:
    svc = tool(two, ["one"])
    assert asyncio.run(svc.offer(ADMIN)) == [gh.github_tool()]
    out = call(svc, what="branches")
    assert out["repo"] == "acme/one"
    assert out["branches"][0]["name"] == "main"


def test_with_two_repositories_the_name_decides_and_its_own_token_is_used(two: TwoRepos) -> None:
    svc = tool(two, ["one", "two"])
    with pytest.raises(ToolError) as e:
        call(svc, what="branches")
    assert e.value.code == "repo_needed" and "one" in e.value.message and "two" in e.value.message
    assert call(svc, what="branches", repo="two")["branches"][0]["name"] == "dev"
    assert call(svc, what="branches", repo="acme/one")["repo"] == "acme/one"
    seen = {(path, auth) for _, path, auth in two.requests}
    assert ("/repos/acme/two/branches", f"Bearer {OTHER_TOKEN}") in seen
    assert ("/repos/acme/one/branches", f"Bearer {TOKEN}") in seen
    assert {m for m, _, _ in two.requests} == {"GET"}
    with pytest.raises(ToolError) as e:
        call(svc, what="branches", repo="nope")
    assert e.value.code == "unknown_repo"


def test_without_any_repository_the_tool_is_not_offered(two: TwoRepos) -> None:
    svc = tool(two, [])
    assert asyncio.run(svc.offer(ADMIN)) == []
    with pytest.raises(ToolError) as e:
        call(svc, what="branches")
    assert e.value.status == 503


# --- with the database (the API) -----------------------------------------------------------------


class AddFake(FakeGitHub):
    """Knows the repository's own data besides the tool's routes."""

    push = True

    def route(self, path: str, query: str) -> tuple[int, Any]:
        if path == "/repos/acme/app":
            return 200, {
                "default_branch": "trunk",
                "private": True,
                "pushed_at": "2026-10-09T00:00:00Z",
                "permissions": {"push": self.push},
            }
        if path == "/repos/acme/app/branches":
            return 200, [
                {"name": "trunk", "commit": {"sha": "a" * 40}},
                {"name": "topcoat/try-it", "commit": {"sha": "b" * 40}},
            ]
        if path == "/repos/acme/app/pulls":
            return 200, [{"number": 1}, {"number": 2}]
        if path == "/repos/acme/app/contents/README.md":
            import base64

            return 200, {
                "type": "file",
                "path": "README.md",
                "sha": "s",
                "size": 3,
                "encoding": "base64",
                "content": base64.b64encode(b"hi\n").decode(),
            }
        return 404, {"message": "Not Found"}


@pytest.fixture
def github() -> Iterator[AddFake]:
    f = AddFake()
    yield f
    f.server.shutdown()


@pytest.fixture
def hub(settings: Settings, github: AddFake) -> Iterator[tuple[TestClient, FastAPI]]:
    app = create_app(settings.model_copy(update={"github_api_url": github.url}))
    with TestClient(app) as c:
        yield c, app


BODY = {
    "name": "app",
    "repo": "https://github.com/acme/app.git",
    "token": TOKEN,
    "mode": "read",
    "test_template": "make-check",
}


def test_adding_a_repository_reads_it_and_never_shows_the_token(
    hub: tuple[TestClient, FastAPI], github: AddFake, clean_db: str
) -> None:
    client, _ = hub
    r = client.post("/api/dev/repos", json=BODY, headers=AUTH)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["full_name"] == "acme/app" and body["default_branch"] == "trunk"
    assert body["state"]["open_prs"] == 2
    assert body["state"]["test_branches"] == ["topcoat/try-it"]
    assert TOKEN not in r.text
    listing = client.get("/api/dev/repos", headers=AUTH)
    assert TOKEN not in listing.text
    assert listing.json()["repos"][0]["token_set"] is True
    # in the database the token is sealed
    engine = create_engine(clean_db)
    with engine.connect() as conn:
        sealed = conn.scalar(text("SELECT token_enc FROM dev_repos"))
    engine.dispose()
    assert sealed and TOKEN not in sealed
    assert {m for m, _, _ in github.requests} == {"GET"}


def test_a_bad_request_is_refused_before_anything_is_stored(
    hub: tuple[TestClient, FastAPI], github: AddFake
) -> None:
    client, _ = hub
    for patch, code in (
        ({"repo": "https://evil.example/a/b"}, "invalid_repo"),
        ({"mode": "write"}, "invalid_mode"),
        ({"test_template": "rm -rf /"}, "invalid_template"),
        ({"repo": "acme/missing"}, "not_found"),
    ):
        r = client.post("/api/dev/repos", json={**BODY, **patch}, headers=AUTH)
        assert r.status_code == 422 and r.json()["detail"]["code"] == code, (patch, r.text)
    github.push = False
    r = client.post("/api/dev/repos", json={**BODY, "mode": "test-branch"}, headers=AUTH)
    assert r.json()["detail"]["code"] == "token_cannot_push"
    assert client.get("/api/dev/repos", headers=AUTH).json()["repos"] == []
    assert client.post("/api/dev/repos", json=BODY, headers=AUTH).status_code == 201
    assert client.post("/api/dev/repos", json=BODY, headers=AUTH).status_code == 409


def test_only_the_admin_token_opens_it_and_rows_belong_to_their_person(
    hub: tuple[TestClient, FastAPI], clean_db: str
) -> None:
    client, _ = hub
    _, key = make_user(clean_db, "guest", role="admin")
    for call in (
        lambda h: client.get("/api/dev/repos", headers=h),
        lambda h: client.post("/api/dev/repos", json=BODY, headers=h),
    ):
        assert call(key).status_code == 403  # an API key, even of an admin, never opens it
        assert call({}).status_code == 401
    assert client.post("/api/dev/repos", json=BODY, headers=AUTH).status_code == 201


def test_refresh_and_remove(hub: tuple[TestClient, FastAPI], github: AddFake) -> None:
    client, _ = hub
    client.post("/api/dev/repos", json=BODY, headers=AUTH)
    r = client.post("/api/dev/repos/app/refresh", headers=AUTH)
    assert r.status_code == 200 and r.json()["state"]["branches"] == 2
    assert client.post("/api/dev/repos/nope/refresh", headers=AUTH).status_code == 404
    assert client.delete("/api/dev/repos/app", headers=AUTH).status_code == 204
    assert client.get("/api/dev/repos", headers=AUTH).json()["repos"] == []
    assert client.delete("/api/dev/repos/app", headers=AUTH).status_code == 404


def test_a_repository_can_be_edited_without_adding_it_again(
    hub: tuple[TestClient, FastAPI], github: AddFake, clean_db: str
) -> None:
    client, _ = hub
    client.post("/api/dev/repos", json=BODY, headers=AUTH)
    r = client.patch(
        "/api/dev/repos/app", json={"mode": "test-branch", "test_template": "pytest"}, headers=AUTH
    )
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "test-branch" and r.json()["test_template"] == "pytest"
    # a new token replaces the sealed one and is never shown
    r = client.patch("/api/dev/repos/app", json={"token": OTHER_TOKEN}, headers=AUTH)
    assert r.status_code == 200 and OTHER_TOKEN not in r.text
    engine = create_engine(clean_db)
    with engine.connect() as conn:
        sealed = conn.scalar(text("SELECT token_enc FROM dev_repos"))
    engine.dispose()
    assert sealed and OTHER_TOKEN not in sealed and TOKEN not in sealed
    assert {m for m, _, _ in github.requests} == {"GET"}
    # a bad value or a token that may not push changes nothing
    assert (
        client.patch("/api/dev/repos/app", json={"mode": "write"}, headers=AUTH).status_code == 422
    )
    github.push = False
    r = client.patch("/api/dev/repos/app", json={"mode": "read"}, headers=AUTH)
    assert r.status_code == 200
    r = client.patch("/api/dev/repos/app", json={"mode": "test-branch"}, headers=AUTH)
    assert r.json()["detail"]["code"] == "token_cannot_push"
    assert client.get("/api/dev/repos", headers=AUTH).json()["repos"][0]["mode"] == "read"
    assert (
        client.patch("/api/dev/repos/nope", json={"mode": "read"}, headers=AUTH).status_code == 404
    )


def test_the_tool_reads_a_registered_repository(
    hub: tuple[TestClient, FastAPI], github: AddFake
) -> None:
    client, app = hub
    client.post("/api/dev/repos", json=BODY, headers=AUTH)
    caller = type(
        "C", (), {"user_id": uuid.UUID("00000000-0000-4000-8000-000000000001"), "admin": True}
    )()
    out = asyncio.run(
        app.state.github.run_tool(caller, gh.CAPABILITY, {"what": "file", "path": "README.md"})
    )["output"]
    assert out["repo"] == "acme/app" and out["text"] == "hi"
    assert {m for m, _, _ in github.requests} == {"GET"}


def test_a_repository_is_found_however_the_caller_spells_it() -> None:
    import asyncio
    import types
    import uuid

    from topcoat_hub.devrepos import DevRepoStore

    rows = [
        types.SimpleNamespace(id=uuid.uuid4(), name="topcoat", full_name="Acme/sloppy-topcoat"),
        types.SimpleNamespace(id=uuid.uuid4(), name="test", full_name="Acme/test"),
    ]
    store = DevRepoStore.__new__(DevRepoStore)

    async def listing(_user):
        return rows

    store.list = listing  # type: ignore[method-assign]

    def find(ref: str):
        row = asyncio.run(store.find(uuid.uuid4(), ref))
        return row.name if row else None

    assert find("topcoat") == "topcoat"
    assert find("acme/sloppy-topcoat") == "topcoat"
    assert find("https://github.com/Acme/sloppy-topcoat.git") == "topcoat"
    assert find("sloppy-topcoat") == "topcoat"
    assert find("nothing") is None
