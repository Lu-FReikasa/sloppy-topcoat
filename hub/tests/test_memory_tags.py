"""The tag tree of the memory (ARCHITECTURE D74): a fixed top level, tags that are found by what
they mean, inheritance of who is responsible and what is sensitive, the changes a person makes,
and the tags an assistant gives a note when it writes one."""

import uuid
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.test_chat import new_conv
from tests.test_memory import chat_once, memory_items, note, setup_node
from tests.users import make_user
from topcoat_hub.memory import EXTRACTION_SYSTEM
from topcoat_hub.memory_tags import ROOT_NAMES, TagTree, clean_name, default_tag, fold
from topcoat_hub.models import DEV_ADMIN_ID


def tree(client: TestClient, headers: dict[str, str] = AUTH) -> dict[str, dict[str, Any]]:
    r = client.get("/api/memory/tags", headers=headers)
    assert r.status_code == 200, r.text
    return {t["name"]: t for t in r.json()["tags"]}


def add(client: TestClient, name: str, parent: str, **kw: Any) -> Any:
    return client.post(
        "/api/memory/tags",
        headers=AUTH,
        json={"name": name, "parent_id": tree(client)[parent]["id"], **kw},
    )


def on_tree(client: TestClient, fn: Any) -> Any:
    sessionmaker = client.app.state.sessionmaker

    async def run() -> Any:
        async with sessionmaker() as db:
            t = TagTree(db, DEV_ADMIN_ID)
            await t.ensure()
            out = await fn(t)
            await db.commit()
            return out

    return client.portal.call(run)


def test_names_are_cleaned_and_compared_by_meaning() -> None:
    assert clean_name("  Home Lab ") == "home-lab"
    assert clean_name("Gerät") == "gerät"
    assert clean_name("bad tag!") is None and clean_name("") is None
    assert fold("Container") == fold("containers") == fold("container")
    assert fold("Geräte") == fold("geraet")
    assert fold("docker") != fold("dock")
    assert default_tag("person", None) == "person"
    assert default_tag("preference", None) == "preferences"
    assert default_tag("operational", "admin") == "infrastructure"
    assert default_tag("operational", "unknown-program") == "everyday"


def test_every_person_starts_with_the_fixed_top_level(client: TestClient) -> None:
    tags = tree(client)
    roots = [n for n, t in tags.items() if t["root"]]
    assert roots == list(ROOT_NAMES)  # the fixed top level in its own order
    assert tags["infrastructure"]["effective"]["responsible"] == "admin"
    assert tags["development"]["effective"]["responsible"] == "dev"
    assert tags["person"]["effective"]["responsible"] == "person"
    # the old "hidden everywhere" tags are a sensitive branch below person
    for name in ("health", "finance", "private"):
        assert tags[name]["parent_id"] == tags["person"]["id"]
        assert tags[name]["effective"]["sensitive"] is True
    assert tags["everyday"]["effective"]["sensitive"] is False


def test_a_child_inherits_until_it_says_otherwise(client: TestClient) -> None:
    assert add(client, "docker", "infrastructure").status_code == 201
    assert add(client, "compose", "docker").status_code == 201
    tags = tree(client)
    assert tags["compose"]["effective"]["responsible"] == "admin"
    # a branch of the machines that Development looks after
    r = client.patch(
        f"/api/memory/tags/{tags['docker']['id']}", headers=AUTH, json={"responsible": "dev"}
    )
    assert r.status_code == 200, r.text
    tags = tree(client)
    assert tags["compose"]["effective"]["responsible"] == "dev"
    assert tags["infrastructure"]["effective"]["responsible"] == "admin"
    # sensitive and released are handed down the same way
    client.patch(
        f"/api/memory/tags/{tags['docker']['id']}",
        headers=AUTH,
        json={"sensitive": True, "released": ["admin", "nonsense"]},
    )
    tags = tree(client)
    assert tags["compose"]["effective"]["sensitive"] is True
    assert tags["compose"]["effective"]["released"] == ["admin"]


def test_the_tree_refuses_what_would_break_it(client: TestClient) -> None:
    tags = tree(client)
    # the top level is fixed
    r = client.patch(f"/api/memory/tags/{tags['person']['id']}", headers=AUTH, json={"name": "me"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "top_level"
    r = client.delete(f"/api/memory/tags/{tags['person']['id']}", headers=AUTH)
    assert r.json()["detail"]["code"] == "top_level"
    # a name that means one that exists is that tag
    add(client, "server", "infrastructure")
    r = add(client, "Servers", "infrastructure")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "exists"
    # four levels at most
    add(client, "rack", "server")
    add(client, "shelf", "rack")
    r = add(client, "slot", "shelf")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "too_deep"
    # no cycles
    tags = tree(client)
    r = client.patch(
        f"/api/memory/tags/{tags['server']['id']}",
        headers=AUTH,
        json={"parent_id": tags["shelf"]["id"]},
    )
    assert r.json()["detail"]["code"] == "cycle"
    r = client.patch(
        f"/api/memory/tags/{tags['server']['id']}", headers=AUTH, json={"responsible": "nobody"}
    )
    assert r.json()["detail"]["code"] == "responsible"


def test_merging_and_deleting_keep_every_note(client: TestClient) -> None:
    add(client, "container", "infrastructure")
    add(client, "podman", "infrastructure")
    add(client, "pods", "podman")
    tags = tree(client)
    from tests.test_memory_applications import seed

    note_id = seed(
        "alpha runs in podman", application="admin", tags=[uuid.UUID(tags["podman"]["id"])]
    )
    r = client.post(
        f"/api/memory/tags/{tags['podman']['id']}/merge",
        headers=AUTH,
        json={"into": tags["container"]["id"]},
    )
    assert r.status_code == 200, r.text
    tags = tree(client)
    assert "podman" not in tags and tags["pods"]["parent_id"] == tags["container"]["id"]
    assert client.get(f"/api/memory/{note_id}", headers=AUTH).json()["tags"] == ["container"]
    r = client.delete(f"/api/memory/tags/{tags['container']['id']}", headers=AUTH)
    assert r.status_code == 200
    tags = tree(client)
    assert tags["pods"]["parent_id"] == tags["infrastructure"]["id"]
    assert client.get(f"/api/memory/{note_id}", headers=AUTH).json()["tags"] == ["infrastructure"]


def test_who_decides_is_the_deepest_tag_and_sensitive_goes_to_the_person(
    client: TestClient,
) -> None:
    add(client, "dns", "infrastructure")
    add(client, "repos", "development")

    async def decide(t: TagTree) -> list[str | None]:
        ids = {x.name: x.id for x in await t.all()}
        return [
            await t.responsible_for([ids["dns"]]),
            await t.responsible_for([ids["dns"], ids["everyday"]]),  # the deeper one decides
            await t.responsible_for([ids["dns"], ids["repos"]]),  # same depth, two programs
            await t.responsible_for([ids["dns"], ids["health"]]),  # sensitive: the person
            await t.responsible_for([]),
        ]

    assert on_tree(client, decide) == ["admin", "admin", None, "person", None]


def test_resolve_uses_what_exists_and_makes_little(client: TestClient) -> None:
    async def resolve(t: TagTree) -> list[str]:
        ids = await t.resolve(
            [
                "Infrastructure",  # a known one, any spelling
                "infrastructure/nas",  # new below a known one
                "infrastructure/ups",
                "infrastructure/vpn",  # a third new one in one note: too many
                "nowhere/thing",  # an unknown parent
                "/lonely",  # no parent: no new top level
                42,
            ],
            by="agent",
            allow_new=True,
        )
        return sorted([(await t.get(i)).name for i in ids])  # type: ignore[union-attr]

    assert on_tree(client, resolve) == ["infrastructure", "nas", "ups"]

    async def fallback(t: TagTree) -> list[str]:
        ids = await t.resolve(["unknown"], by="agent", allow_new=False, fallback="everyday")
        return [(await t.get(i)).name for i in ids]  # type: ignore[union-attr]

    assert on_tree(client, fallback) == ["everyday"]


def test_the_tree_is_personal(client: TestClient, clean_db: str) -> None:
    _, other = make_user(clean_db, "user-b")
    add(client, "garden", "everyday")
    assert "garden" not in tree(client, other)
    mine = tree(client)["garden"]["id"]
    assert client.delete(f"/api/memory/tags/{mine}", headers=other).status_code == 404
    assert "garden" in tree(client)


def test_the_assistant_tags_what_it_writes_from_the_tag_list(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    seen: list[str] = []

    def answer(req: dict[str, Any]) -> str | None:
        if req.get("stream"):
            return None
        if req["messages"][0]["content"] == EXTRACTION_SYSTEM:
            seen.append(req["messages"][-1]["content"])
            import json

            return json.dumps(
                {
                    "memories": [
                        note(
                            "Der NAS sichert jede Nacht",
                            kind="operational",
                            tags=["infrastructure/nas"],
                        ),
                        note("Mag kurze Antworten", kind="preference", tags=["preferences"]),
                    ]
                }
            )
        return None

    node.answer_fn = answer
    conv = client.post("/api/conversations", headers=AUTH, json={"application": "admin"}).json()[
        "id"
    ]
    chat_once(client, "Mein NAS sichert jede Nacht, und ich mag kurze Antworten.", conv)
    assert seen and "Tag list:" in seen[0] and "infrastructure — Servers" in seen[0]
    items = {i["text"]: i for i in memory_items(client, application="admin")}
    assert items["Der NAS sichert jede Nacht"]["tags"] == ["nas"]
    core = {i["text"]: i for i in memory_items(client, application="core")}
    assert core["Mag kurze Antworten"]["tags"] == ["preferences"]
    nas = tree(client)["nas"]
    assert nas["created_by"] == "agent" and nas["reviewed"] is False
    # a new conversation lists the new tag for the next note
    chat_once(client, "Mein NAS sichert jede Nacht, wirklich.", new_conv(client))
    assert "infrastructure/nas" in seen[-1]
