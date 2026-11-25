"""The everyday measurements of the models (SCH-06): speeds, counts and times per application, model
and node, read from the record of requests. Figures only."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.conftest import AUTH
from topcoat_hub.models import JobRecord


@pytest.fixture(autouse=True)
def _empty_record(migrated_db: str) -> None:
    engine = create_engine(migrated_db)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM job_records"))
    engine.dispose()


def record(db_url: str, **values: Any) -> None:
    base = {
        "id": uuid.uuid4(),
        "created_at": datetime.now(UTC),
        "work": "chat",
        "caller": "",
        "requested": "",
        "chosen_model": "big",
        "chosen_node": "node-a",
        "place": 0,
        "source": "picked",
        "reason": "",
        "outcome": "done",
        "waited_ms": 0,
        "model_loaded": True,
        "duration_ms": 0,
        "application": "chat",
    }
    row = {**base, **values}
    engine = create_engine(db_url)
    with Session(engine) as session:
        session.add(JobRecord(**row))
        session.commit()
    engine.dispose()


def get(client: TestClient, days: int = 7) -> list[dict[str, Any]]:
    r = client.get(f"/api/scheduling/measurements?days={days}", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["rows"]


def test_speeds_are_tokens_over_the_time_they_took(client: TestClient, migrated_db: str) -> None:
    record(
        migrated_db,
        tokens=100,
        predicted_ms=2000,
        prompt_tokens=500,
        cached_tokens=300,
        prompt_ms=1000,
    )
    record(
        migrated_db,
        tokens=300,
        predicted_ms=4000,
        prompt_tokens=500,
        cached_tokens=100,
        prompt_ms=3000,
        waited_ms=4000,
    )
    (row,) = get(client)
    assert (row["application"], row["model"], row["node"]) == ("chat", "big", "node-a")
    assert row["requests"] == 2
    assert row["generated_tps"] == round(400 * 1000 / 6000, 1)
    assert row["prompt_tps"] == round(600 * 1000 / 4000, 1)  # only what was read, not the cache
    assert row["cached_share"] == round(400 / 1000, 2)
    assert row["waited_seconds"] == 2.0  # the mean of 0 s and 4 s


def test_failures_aborts_and_cut_off_answers_are_counted(
    client: TestClient, migrated_db: str
) -> None:
    record(migrated_db)
    record(migrated_db, outcome="failed", error_code="timeout")
    record(migrated_db, outcome="cancelled")
    record(migrated_db, finish_reason="length", tokens=10, predicted_ms=100)
    (row,) = get(client)
    assert (row["requests"], row["failed"], row["cancelled"], row["cut_off"]) == (4, 1, 1, 1)


def test_applications_models_and_nodes_are_told_apart(client: TestClient, migrated_db: str) -> None:
    record(migrated_db)
    record(migrated_db, application="dev")
    record(migrated_db, chosen_node="node-b")
    record(migrated_db, chosen_model="small")
    assert len(get(client)) == 4


def test_old_records_are_left_out_and_so_is_a_request_that_never_reached_a_node(
    client: TestClient, migrated_db: str
) -> None:
    record(migrated_db, created_at=datetime.now(UTC) - timedelta(days=30))
    record(migrated_db, chosen_node="")  # refused before a node was chosen
    assert get(client, days=7) == []
    assert len(get(client, days=60)) == 1


def test_a_row_without_figures_has_none_and_the_view_names_no_person(
    client: TestClient, migrated_db: str
) -> None:
    record(migrated_db, user_id=None, conversation_id=uuid.uuid4())
    (row,) = get(client)
    assert row["generated_tps"] is None and row["prompt_tps"] is None
    assert not {"user_id", "conversation_id", "task_id", "caller"} & set(row)


def test_only_an_admin_reads_the_measurements(client: TestClient) -> None:
    assert client.get("/api/scheduling/measurements").status_code == 401


def test_model_switches_count_loads_and_changes_per_node(
    client: TestClient, migrated_db: str
) -> None:
    """P2a: a load is a request whose model was not loaded; a change is another model than the
    node served last. Figures only, read from the same record."""
    record(migrated_db, chosen_node="node-a", chosen_model="big", model_loaded=False, work="chat")
    record(
        migrated_db,
        chosen_node="node-a",
        chosen_model="small",
        model_loaded=False,
        work="review",
        waited_ms=2500,
    )
    record(
        migrated_db, chosen_node="node-a", chosen_model="small", model_loaded=True, work="review"
    )
    r = client.get("/api/scheduling/model-switches?days=7", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["requests"] == 3
    assert body["totals"]["loads"] == 2
    assert body["totals"]["switches"] == 1
    assert body["totals"]["review_loads"] == 1
    assert body["totals"]["waited_seconds_on_loads"] == 2.5
    assert body["review_load_share"] == 0.5
    (cell,) = body["cells"]
    assert (cell["node"], cell["loads"], cell["switches"]) == ("node-a", 2, 1)


def test_the_choice_switch_takes_only_alt_or_neu_and_defaults_to_alt(client: TestClient) -> None:
    r = client.get("/api/scheduling/model-switches?days=7", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["choice_mode"] == "alt"
    bad = client.put("/api/scheduling/choice-mode", json={"mode": "maybe"}, headers=AUTH)
    assert bad.status_code == 422
    good = client.put("/api/scheduling/choice-mode", json={"mode": "neu"}, headers=AUTH)
    assert good.status_code == 200, good.text
    assert good.json() == {"mode": "neu"}
    assert (
        client.get("/api/scheduling/model-switches?days=7", headers=AUTH).json()["choice_mode"]
        == "neu"
    )
    client.put("/api/scheduling/choice-mode", json={"mode": "alt"}, headers=AUTH)
