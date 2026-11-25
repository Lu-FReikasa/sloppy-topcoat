# ruff: noqa: E501  (note texts and names are long)
"""The knowledge base (E2-03 slim): indexing through a tool node, the exclude list and secret
filter, hybrid search, visibility per user (D20), and the agent's ``knowledge.search`` tool.

The tool node is the protocol-level fake; its ``docs.list`` / ``docs.read`` answer from a dict of
files, in small pages so paging is exercised, and — to prove the hub's own list — it can be told
to list files a real node would never list."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import FakeNode, eventually, pair, tools_inventory
from tests.test_agent import finish, tool, use, wait_for
from tests.test_tools import grant
from tests.users import make_user
from topcoat_hub.embedding import EmbeddingError
from topcoat_hub.knowledge import SEARCH_CAPABILITY, KnowledgeService

ROOT = "/srv/notes"
PAGE = 120  # bytes per docs.read page, so every note needs several calls

NOTES = {
    "README.md": "# Home lab\n\nThe web stack runs on vm-docker1 in the compose project site.\n\n"
    "## Backups\n\nThe nightly backup of the database runs at 03:00 and keeps seven days of dumps.\n",
    "servers/web.md": "# Web\n\n## Restart\n\nThe web container restarts itself when it runs out "
    "of memory; raise the memory limit in the compose file and restart the stack.\n\n"
    "## Certificates\n\nThe certificate of the reverse proxy is renewed every sixty days.\n",
    "servers/nas.txt": "The NAS exports the share media and the share backup over NFS to the docker hosts.",
}


class Docs:
    """The files of the fake node, and a log of what the hub asked for."""

    def __init__(self) -> None:
        self.files: dict[str, str] = dict(NOTES)
        self.mtime = 1_700_000_000
        self.reads: list[str] = []
        self.lists = 0
        self.also_list: list[str] = []  # paths a misbehaving node lists although it should not
        self.refuse_root = False

    def list(self, inp: dict[str, Any]) -> dict[str, Any]:
        self.lists += 1
        if self.refuse_root or inp.get("root") != ROOT:
            return {
                "error": {"code": "capability_failed", "message": "that directory is not released"}
            }
        paths = sorted({*self.files, *self.also_list})
        after, limit = inp.get("after", ""), inp.get("limit", 500)
        rest = [p for p in paths if p > after]
        page = rest[:limit]
        out: dict[str, Any] = {
            "files": [
                {"path": p, "size": len(self.files.get(p, "x")), "mtime": self.mtime} for p in page
            ],
            "total": len(paths),
            "excluded": 2,
            "truncated": len(rest) > limit,
        }
        if out["truncated"]:
            out["next_after"] = page[-1]
        return out

    def read(self, inp: dict[str, Any]) -> dict[str, Any]:
        path, offset = inp["path"], int(inp.get("offset", 0))
        self.reads.append(path)
        text = self.files.get(path, "")
        chunk = text[offset : offset + PAGE]
        more = offset + PAGE < len(text)
        return {
            "path": path, "offset": offset, "size": len(text), "mtime": self.mtime,
            "content": chunk, "masked": 0, "next_offset": offset + PAGE if more else None,
        }  # fmt: skip


def docs_inventory() -> dict[str, Any]:
    inv = tools_inventory()
    schema = {"type": "object", "additionalProperties": True, "properties": {}}
    for name in ("docs.list", "docs.read"):
        inv["capabilities"].append(
            {
                "name": name,
                "description": f"{name} (test)",
                "read_only": True,
                "input_schema": schema,
            }
        )
    return inv


@pytest.fixture
def world(client: TestClient, connect) -> dict[str, Any]:
    """A connected tool node that offers docs.list / docs.read over a dict of files."""
    node_id, key = pair(client, "vm-docker", role="tools")
    node: FakeNode = connect(node_id, key)
    docs = Docs()
    node.inventory = docs_inventory()
    node.capability_answers["docs.list"] = docs.list
    node.capability_answers["docs.read"] = docs.read
    node.capability_answers["system.info"] = {"hostname": "vm"}
    node.handshake()
    node.serve()

    def stored() -> None:
        offered = client.get(f"/api/nodes/{node_id}/capabilities", headers=AUTH).json()["offered"]
        assert any(c["name"] == "docs.read" for c in offered)

    eventually(stored)
    return {"node_id": node_id, "node": node, "docs": docs}


def add_source(client: TestClient, name: str = "Notes", visibility: str = "shared", who=AUTH, **kw):
    body = {"name": name, "node": "vm-docker", "root": ROOT, "visibility": visibility, **kw}
    return client.post("/api/knowledge/sources", headers=who, json=body)


def ready(client: TestClient, source_id: str, who=AUTH, status: str = "ready") -> dict[str, Any]:
    def check() -> dict[str, Any]:
        s = client.get(f"/api/knowledge/sources/{source_id}", headers=who).json()
        assert s["status"] == status, (s["status"], s.get("error"))
        return s

    return eventually(check, timeout=10)


def search(client: TestClient, q: str, who=AUTH, **params: Any) -> list[dict[str, Any]]:
    r = client.get("/api/knowledge/search", headers=who, params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r.json()["hits"]


def indexed(client: TestClient, world: dict[str, Any], **kw: Any) -> dict[str, Any]:
    r = add_source(client, **kw)
    assert r.status_code == 201, r.text
    return ready(client, r.json()["id"])


def test_a_source_is_indexed_through_the_node_in_pages(
    client: TestClient, world: dict[str, Any]
) -> None:
    source = indexed(client, world)
    assert source["files"] == 3 and source["chunks"] >= 5
    assert source["node"] == "vm-docker" and source["own"] is True and source["excluded"] >= 2
    docs: Docs = world["docs"]
    assert docs.reads.count("servers/web.md") >= 2  # the note is longer than one page
    hits = search(client, "backup of the database at night")
    assert hits and hits[0]["path"] == "README.md" and hits[0]["heading"] == "Home lab > Backups"
    assert "03:00" in hits[0]["text"] and hits[0]["source"] == "Notes"


def test_search_finds_a_paged_note_whole_and_by_heading(
    client: TestClient, world: dict[str, Any]
) -> None:
    indexed(client, world)
    hits = search(client, "web container runs out of memory restart")
    assert hits[0]["path"] == "servers/web.md" and hits[0]["heading"] == "Web > Restart"
    assert "raise the memory limit" in hits[0]["text"]  # text from the second page is there
    assert search(client, "NFS share media")[0]["path"] == "servers/nas.txt"
    assert search(client, "zzzunknownwordzzz") == []
    only = search(client, "backup", limit=1)
    assert len(only) == 1


def test_the_hub_never_asks_for_a_path_on_the_exclude_list(
    client: TestClient, world: dict[str, Any]
) -> None:
    docs: Docs = world["docs"]
    bad = [
        "SENSIBEL/logins.md", "servers/nas-password.md", "keys/host.key", ".git/config.md",
        "Zugangsdaten-SENSIBEL.md", "notes/secrets-of-cooking.md", "deploy/.env.production",
    ]  # fmt: skip
    docs.also_list = bad
    for p in bad:
        docs.files[p] = "# never\nadmin password: hunter2hunter2 and the word backup"
    source = indexed(client, world)
    assert not set(docs.reads) & set(bad), docs.reads
    assert (
        source["files"] == 3 and source["excluded"] >= 2 + len(bad) - 2
    )  # .key is not text either
    for hit in search(client, "backup hunter2hunter2 password"):
        assert hit["path"] not in bad and "hunter2" not in hit["text"]


def test_a_later_addition_to_the_exclude_list_hides_old_hits(
    client: TestClient, world: dict[str, Any]
) -> None:
    indexed(client, world)
    assert any(h["path"].startswith("servers/") for h in search(client, "certificate proxy"))
    client.app.state.settings.knowledge_exclude = "servers"
    assert search(client, "certificate proxy") == []
    status = client.get("/api/knowledge/status", headers=AUTH).json()
    assert "servers" in status["exclude"] and "*SENSIBEL*" in status["exclude"]


def test_a_chunk_with_a_secret_is_held_back_and_only_its_rule_is_reported(
    client: TestClient, world: dict[str, Any]
) -> None:
    docs: Docs = world["docs"]
    docs.files["ops.md"] = (
        "# Ops\n\n## Login\n\nThe admin password is hunter2hunter2 for the router login page.\n\n"
        "## Dashboard\n\nSee https://example.invalid/d/0123456789abcdefghijklmnopqrstuvwxyz0123 "
        "for the dashboard of the backup jobs.\n"
    )
    source = indexed(client, world)
    assert source["blocked"] == 1
    detail = client.get(f"/api/knowledge/sources/{source['id']}", headers=AUTH).json()
    assert detail["blocked_files"] == [{"path": "ops.md", "rules": ["password"], "chunks": 1}]
    assert search(client, "router login password") == []
    hits = search(client, "dashboard of the backup jobs")
    assert hits and hits[0]["path"] == "ops.md"
    assert "0123456789abcdef" not in hits[0]["text"] and "for the dashboard" in hits[0]["text"]


def test_reindex_reads_only_what_changed_and_drops_what_vanished(
    client: TestClient, world: dict[str, Any]
) -> None:
    docs: Docs = world["docs"]
    source = indexed(client, world)
    docs.reads.clear()
    client.post(f"/api/knowledge/sources/{source['id']}/reindex", headers=AUTH)
    ready(client, source["id"])
    assert docs.reads == []  # nothing changed: nothing is read again
    docs.files["servers/nas.txt"] = (
        "The NAS now exports the share archive over SMB to the docker hosts."
    )
    docs.mtime += 60
    del docs.files["README.md"]
    client.post(f"/api/knowledge/sources/{source['id']}/reindex", headers=AUTH)
    changed = eventually(lambda: _changed(client, source["id"]))
    assert changed["files"] == 2
    assert set(docs.reads) == {"servers/nas.txt", "servers/web.md"}  # mtime moved for both
    assert search(client, "archive SMB")[0]["path"] == "servers/nas.txt"
    assert search(client, "NFS") == []
    assert search(client, "nightly backup of the database") == []  # README.md is gone


def _changed(client: TestClient, source_id: str) -> dict[str, Any]:
    s = client.get(f"/api/knowledge/sources/{source_id}", headers=AUTH).json()
    assert s["status"] == "ready" and s["files"] == 2, s
    return s


def test_unchanged_content_with_a_new_time_is_not_indexed_again(
    client: TestClient, world: dict[str, Any]
) -> None:
    docs: Docs = world["docs"]
    source = indexed(client, world)
    docs.mtime += 1  # touched, not edited
    docs.reads.clear()
    client.post(f"/api/knowledge/sources/{source['id']}/reindex", headers=AUTH)
    ready(client, source["id"])
    assert set(docs.reads) == set(NOTES)  # read again to compare ...
    assert ready(client, source["id"])["chunks"] == source["chunks"]  # ... nothing rewritten


class FlakyEmbedder:
    """An embedder that is down for the first calls, then works."""

    id = "fake-flaky"
    dup_threshold, recall_min, cluster_threshold = 0.9, 0.0, 0.5

    def __init__(self) -> None:
        self.up = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.up:
            raise EmbeddingError("the embedding model is not loaded")
        return [[1.0 if i == j else 0.0 for j in range(8)] for i, _ in enumerate(texts)]


def test_without_an_embedding_model_search_still_works_by_keywords_and_vectors_follow(
    client: TestClient, world: dict[str, Any]
) -> None:
    app = client.app
    embedder = FlakyEmbedder()
    app.state.knowledge = KnowledgeService(
        app.state.sessionmaker, app.state.tools, embedder, app.state.settings
    )
    app.state.embedder = embedder

    def embedded() -> int:
        return int(client.get("/api/knowledge/sources", headers=AUTH).json()[0]["embedded"])

    source = indexed(client, world)
    assert search(client, "backup of the database")[0]["path"] == "README.md"  # keywords only
    assert embedded() == 0  # the list says so: chunks without a vector of the embedder in use
    import sqlalchemy as sa

    def vectors() -> int:
        engine = sa.create_engine(TEST_DB_URL)
        with engine.connect() as conn:
            n = conn.scalar(
                sa.text("SELECT count(*) FROM knowledge_chunks WHERE embedding IS NOT NULL")
            )
        engine.dispose()
        return int(n or 0)

    assert vectors() == 0
    embedder.up = True
    client.post(f"/api/knowledge/sources/{source['id']}/reindex", headers=AUTH)
    ready(client, source["id"])
    assert vectors() == source["chunks"]  # the next run filled the gap, nothing was read again
    assert embedded() == source["chunks"]


def test_a_source_needs_a_released_directory_and_a_known_node(
    client: TestClient, world: dict[str, Any]
) -> None:
    assert add_source(client, node="nope").status_code == 404
    world["docs"].refuse_root = True
    refused = add_source(client)
    assert refused.status_code == 422 and "not released" in refused.json()["detail"]["message"]
    assert client.get("/api/knowledge/sources", headers=AUTH).json() == []


def test_a_source_whose_node_is_gone_reports_it(client: TestClient, world: dict[str, Any]) -> None:
    source = indexed(client, world)
    import sqlalchemy as sa

    engine = sa.create_engine(TEST_DB_URL)
    with engine.begin() as conn:  # the node is removed (the source's node_id becomes NULL)
        conn.execute(sa.text("DELETE FROM nodes WHERE id = :i"), {"i": world["node_id"]})
    engine.dispose()
    client.post(f"/api/knowledge/sources/{source['id']}/reindex", headers=AUTH)
    failed = ready(client, source["id"], status="error")
    assert "node" in failed["error"]
    assert search(client, "backup")  # what was indexed stays searchable


def test_private_sources_are_the_owners_and_shared_ones_everybodys(
    client: TestClient, world: dict[str, Any]
) -> None:
    _, bob = make_user(TEST_DB_URL, "bob")
    shared = indexed(client, world, name="Shared", visibility="shared")
    docs: Docs = world["docs"]
    docs.files = {"rack-plan.md": "# Plan\n\nThe private plan is to move the backup to a new rack."}
    private = indexed(client, world, name="Mine", visibility="private")
    assert {s["name"] for s in client.get("/api/knowledge/sources", headers=AUTH).json()} == {
        "Shared", "Mine",
    }  # fmt: skip
    seen = client.get("/api/knowledge/sources", headers=bob).json()
    assert [s["name"] for s in seen] == ["Shared"] and seen[0]["own"] is False
    assert seen[0]["root"] is None and seen[0]["error"] is None  # where the files are is not his
    assert seen[0]["embedded"] is None
    assert client.get(f"/api/knowledge/sources/{private['id']}", headers=bob).status_code == 404
    assert client.get(f"/api/knowledge/sources/{shared['id']}", headers=bob).status_code == 200
    assert [h["source"] for h in search(client, "private plan new rack")] == ["Mine"]
    assert all(h["source"] == "Shared" for h in search(client, "backup", who=bob))
    assert search(client, "private plan new rack", who=bob) == []
    assert search(client, "private plan", who=bob, source_id=private["id"]) == []


def test_only_the_admin_changes_sources(client: TestClient, world: dict[str, Any]) -> None:
    _, bob = make_user(TEST_DB_URL, "bob")
    _, admin_key = make_user(TEST_DB_URL, "admin-2", role="admin")
    source = indexed(client, world)
    for who in (bob, admin_key):  # a key is never an admin, even of an admin user
        assert add_source(client, who=who).status_code == 403
        assert (
            client.post(f"/api/knowledge/sources/{source['id']}/reindex", headers=who).status_code
            == 403
        )
        assert (
            client.delete(f"/api/knowledge/sources/{source['id']}", headers=who).status_code == 403
        )
    assert client.delete(f"/api/knowledge/sources/{source['id']}", headers=AUTH).status_code == 204
    assert search(client, "backup") == []  # the chunks went with the source


def test_the_store_scopes_every_statement_by_user(
    client: TestClient, world: dict[str, Any]
) -> None:
    """A guard: a new KnowledgeStore method must be added here, and thus be scoped."""
    import inspect

    from topcoat_hub.knowledge_store import KnowledgeStore

    covered = {
        "create_source", "sources", "get_source", "own_source", "update_source", "delete_source",
        "any_visible_chunk", "files", "drop_files", "replace_file", "touch_file", "unembedded",
        "set_vectors", "counts", "blocked_files", "blocked_overview", "nearest", "keyword",
        "chunks", "chunks_of_path", "embedded_counts",
    }  # fmt: skip
    public = {
        n
        for n, _ in inspect.getmembers(KnowledgeStore, inspect.iscoroutinefunction)
        if not n.startswith("_")
    }
    assert public == covered
    source = indexed(client, world, visibility="private")
    bob_id, _ = make_user(TEST_DB_URL, "bob")
    sm = client.app.state.sessionmaker
    sid = uuid.UUID(source["id"])

    async def as_bob() -> dict[str, Any]:
        async with sm() as db:
            store = KnowledgeStore(db, bob_id)
            found: dict[str, Any] = {
                "sources": list(await store.sources()),
                "get": await store.get_source(sid),
                "own": await store.own_source(sid),
                "files": await store.files(sid),
                "any": await store.any_visible_chunk(),
                "kw": await store.keyword("backup", 10),
                "near": await store.nearest([1.0] * 8, "x", 10),
                "unembedded": await store.unembedded(sid, "x"),
                "embedded": await store.embedded_counts(client.app.state.embedder.id),
                "counts": await store.counts(sid),
                "blocked": await store.blocked_files(sid),
                "overview": await store.blocked_overview(),
                "path": await store.chunks_of_path("README.md"),
            }
            await store.update_source(sid, name="hacked")
            await store.drop_files(sid, ["README.md"])
            found["deleted"] = await store.delete_source(sid)
            return found

    found = client.portal.call(as_bob)
    assert found["sources"] == [] and found["get"] is None and found["own"] is None
    assert found["files"] == {} and found["any"] is False and found["kw"] == []
    assert found["near"] == [] and found["unembedded"] == [] and found["counts"] == (0, 0)
    assert found["blocked"] == [] and found["deleted"] is False and found["embedded"] == {}
    assert found["overview"] == {"chunks": 0, "files": []}
    assert found["path"] == []  # a document of a private source of another person is not read
    again = client.get(f"/api/knowledge/sources/{source['id']}", headers=AUTH).json()
    assert again["name"] == "Notes" and again["files"] == 3


def test_the_periodic_run_indexes_every_source(client: TestClient, world: dict[str, Any]) -> None:
    source = indexed(client, world)
    world["docs"].files["new.md"] = (
        "# New\n\nA brand new note about the cooling of the server rack."
    )
    assert client.portal.call(client.app.state.knowledge.reindex_all) == 1
    eventually(lambda: _has_files(client, source["id"], 4))
    assert search(client, "cooling of the server rack")[0]["path"] == "new.md"


def _has_files(client: TestClient, source_id: str, n: int) -> None:
    s = client.get(f"/api/knowledge/sources/{source_id}", headers=AUTH).json()
    assert s["status"] == "ready" and s["files"] == n, s


# --- the agent's tool ----------------------------------------------------------------------


def test_the_agent_gets_the_search_tool_only_with_notes_and_files_its_hits_as_doc_evidence(
    client: TestClient, world: dict[str, Any]
) -> None:
    # no notes yet: the tool is not offered
    model = use(client, finish("nothing to look up"))
    r = client.post("/api/agent/tasks", headers=AUTH, json={"prompt": "how are backups done?"})
    assert r.status_code == 201
    wait_for(client, r.json()["id"], "done")
    assert SEARCH_CAPABILITY not in model.calls[0][0]["content"]

    indexed(client, world)
    model = use(
        client,
        tool("knowledge", SEARCH_CAPABILITY, query="nightly backup database"),
        lambda messages: finish(
            "The notes say the nightly backup runs at 03:00.",
            findings=[
                {
                    "claim": "the notes say the backup runs at 03:00",
                    "tag": "doc",
                    "evidence": ["E1"],
                    "confirm_with": "look at the cron of the backup job",
                }
            ],
        ),
    )
    r = client.post("/api/agent/tasks", headers=AUTH, json={"prompt": "how are backups done?"})
    done = wait_for(client, r.json()["id"], "done")
    assert SEARCH_CAPABILITY in model.calls[0][0]["content"]
    (ev,) = done["evidence"]
    assert ev["tag"] == "doc" and ev["source"]["capability"] == SEARCH_CAPABILITY
    assert "03:00" in ev["excerpt"] and "README.md" in ev["excerpt"]
    finding = done["diagnosis"]["findings"][0]
    assert finding["tag"] == "doc" and finding["evidence"] == ["E1"] and "downgraded" not in finding


def test_the_agents_search_sees_only_the_users_notes(
    client: TestClient, world: dict[str, Any]
) -> None:
    uid, bob = make_user(TEST_DB_URL, "bob")
    grant(client, world["node_id"], uid, ["system.info"])
    docs: Docs = world["docs"]
    docs.files = {"mine.md": "# Mine\n\nMy private plan is to replace the backup disk in spring."}
    indexed(client, world, name="Mine", visibility="private")
    model = use(
        client,
        tool("knowledge", SEARCH_CAPABILITY, query="private plan backup disk"),
        finish("done"),
    )
    r = client.post("/api/agent/tasks", headers=bob, json={"prompt": "what is planned?"})
    wait_for(client, r.json()["id"], "done", who=bob)
    assert SEARCH_CAPABILITY not in model.calls[0][0]["content"]  # nothing visible to bob
    assert not any("replace the backup disk" in m["content"] for call in model.calls for m in call)


def test_the_search_tool_checks_its_input(client: TestClient, world: dict[str, Any]) -> None:
    indexed(client, world)
    use(
        client,
        tool("knowledge", SEARCH_CAPABILITY, query=""),
        tool("knowledge", SEARCH_CAPABILITY, query="backup", limit=99),
        tool("knowledge", SEARCH_CAPABILITY, query="backup", path="/etc/passwd"),
        finish("done"),
    )
    r = client.post("/api/agent/tasks", headers=AUTH, json={"prompt": "check"})
    done = wait_for(client, r.json()["id"], "done")
    obs = [s["data"].get("text", "") for s in done["steps"] if s["kind"] == "observation"]
    assert sum("invalid_input" in o for o in obs) == 3
    assert done["evidence"] == []


def test_reindex_answers_with_the_source_already_indexing(
    client: TestClient, world: dict[str, Any]
) -> None:
    """Whoever looks right after the call must not read the previous "ready" (the run starts a
    moment later)."""
    source = indexed(client, world)
    r = client.post(f"/api/knowledge/sources/{source['id']}/reindex", headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "indexing"
    assert client.get(f"/api/knowledge/sources/{source['id']}", headers=AUTH).json()["status"] in {
        "indexing",
        "ready",
    }
    ready(client, source["id"])
