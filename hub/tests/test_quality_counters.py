"""How the second look judged the answers of each model in each application (QLT-01): counts only,
a floor per application that marks models and moves nothing."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_chat import ask, new_conv, use_model
from topcoat_hub.models import ChatReview, Conversation, Message


def looks(db_url: str, conv: str, verdicts: list[str], model: str = "small", age_days: int = 0):
    """File one second look per verdict on messages of the conversation (a message holds one)."""
    engine = create_engine(db_url)
    with Session(engine) as session:
        cid = uuid.UUID(conv)
        user_id = session.scalar(select(Conversation.user_id).where(Conversation.id == cid))
        spare = list(
            session.scalars(
                select(Message.id)
                .where(Message.conversation_id == cid)
                .where(~Message.id.in_(select(ChatReview.message_id)))
            )
        )
        assert len(spare) >= len(verdicts), "not enough messages for the looks"
        for message_id, verdict in zip(spare, verdicts, strict=False):
            session.add(
                ChatReview(
                    user_id=user_id,
                    conversation_id=cid,
                    message_id=message_id,
                    verdict=verdict,
                    findings=[],
                    author_model=model,
                    created_at=datetime.now(UTC) - timedelta(days=age_days),
                )
            )
        session.commit()
    engine.dispose()


def world(client: TestClient, connect, answers: int) -> str:
    online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    conv = new_conv(client, application="chat")
    for i in range(answers):
        ask(client, conv, f"frage {i}")
    return conv


def get(client: TestClient, days: int = 30) -> dict[str, Any]:
    r = client.get(f"/api/scheduling/quality?days={days}", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_counters_count_green_orange_and_red_looks(
    client: TestClient, connect, migrated_db: str
) -> None:
    conv = world(client, connect, 4)
    looks(migrated_db, conv, ["ok", "ok", "hint", "error"])
    (row,) = [r for r in get(client)["rows"] if r["model"] == "small"]
    assert (row["application"], row["looks"], row["ok"], row["hint"], row["error"]) == (
        "chat",
        4,
        2,
        1,
        1,
    )
    assert row["ok_share"] == 0.5 and row["floor"] is None and row["below_floor"] is False


def test_a_floor_marks_a_model_only_when_there_are_enough_looks(
    client: TestClient, connect, migrated_db: str
) -> None:
    conv = world(client, connect, 12)
    looks(migrated_db, conv, ["ok", "error", "error"])  # too few to judge by
    put = client.put(
        "/api/scheduling/quality-floor", headers=AUTH, json={"application": "chat", "floor": 0.8}
    )
    assert put.status_code == 200 and put.json()["floors"] == {"chat": 0.8}
    (row,) = [r for r in get(client)["rows"] if r["model"] == "small"]
    assert row["below_floor"] is False  # three looks say too little
    looks(migrated_db, conv, ["ok"] * 3 + ["error"] * 6)  # nine more: twelve in all, 4 green
    (row,) = [r for r in get(client)["rows"] if r["model"] == "small"]
    assert row["looks"] == 12 and row["below_floor"] is True
    # taking the floor away unmarks it
    client.put(
        "/api/scheduling/quality-floor", headers=AUTH, json={"application": "chat", "floor": None}
    )
    (row,) = [r for r in get(client)["rows"] if r["model"] == "small"]
    assert row["floor"] is None and row["below_floor"] is False


def test_old_looks_are_left_out_and_nothing_names_a_person(
    client: TestClient, connect, migrated_db: str
) -> None:
    conv = world(client, connect, 2)
    looks(migrated_db, conv, ["ok"], age_days=90)
    looks(migrated_db, conv, ["hint"])
    (row,) = [r for r in get(client, days=30)["rows"] if r["model"] == "small"]
    assert row["looks"] == 1 and row["hint"] == 1
    assert not {"user_id", "conversation_id", "message_id"} & set(row)


def test_a_floor_must_be_a_share_and_only_an_admin_sets_it(client: TestClient) -> None:
    url = "/api/scheduling/quality-floor"
    assert (
        client.put(url, headers=AUTH, json={"application": "chat", "floor": 1.5}).status_code == 422
    )
    assert client.put(url, json={"application": "chat", "floor": 0.5}).status_code == 401
    assert client.get("/api/scheduling/quality").status_code == 401
