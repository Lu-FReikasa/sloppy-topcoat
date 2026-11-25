"""Research > Recherchieren: a research starts from the application itself (api/research.py) and
runs the shipped program research_report; it is listed while the hub holds it."""

import time
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH


def wait_ended(client: TestClient, seconds: float = 20.0) -> dict[str, Any]:
    end = time.time() + seconds
    while time.time() < end:
        runs = client.get("/api/research/runs", headers=AUTH).json()["runs"]
        if runs and runs[0]["status"] != "running":
            return runs[0]  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError("the research did not end")


def test_a_research_starts_from_the_application_and_is_listed(client: TestClient) -> None:
    r = client.post(
        "/api/research/runs", headers=AUTH, json={"question": "Pixel-Animationen mit lokaler KI?"}
    )
    assert r.status_code == 202, r.text
    started = r.json()
    assert started["question"] == "Pixel-Animationen mit lokaler KI?"
    listed = client.get("/api/research/runs", headers=AUTH).json()["runs"]
    assert [x["id"] for x in listed] == [started["id"]]
    # no node, no model: it ends with a reason instead of hanging
    ended = wait_ended(client)
    assert ended["status"] == "failed" and ended["error"]
    assert ended["report_id"] is None


def test_a_question_is_needed_and_nothing_else_is_taken(client: TestClient) -> None:
    assert (
        client.post("/api/research/runs", headers=AUTH, json={"question": "x"}).status_code == 422
    )
    other = {"question": "Was gibt es Neues?", "program": {"name": "anything"}}
    assert client.post("/api/research/runs", headers=AUTH, json=other).status_code == 422
    assert (
        client.post(
            "/api/research/runs/00000000-0000-0000-0000-000000000000/stop", headers=AUTH
        ).status_code
        == 404
    )


def test_what_a_check_in_research_becomes_by_quality() -> None:
    from topcoat_hub.api.chat import research_mode

    assert research_mode("research", "thorough", False) == "job"
    assert research_mode("research", "balanced", False) == "offer"
    assert research_mode("research", None, False) == "offer"  # balanced is the default
    assert research_mode("research", "fast", False) is None  # a short check as before
    assert research_mode("research", "balanced", True) is None  # the hub's own follow-up
    assert research_mode("chat", "thorough", False) is None


def test_the_offer_starts_a_research_in_its_conversation(client: TestClient) -> None:
    conv = client.post("/api/conversations", headers=AUTH, json={"application": "research"}).json()
    r = client.post(
        f"/api/conversations/{conv['id']}/research",
        headers=AUTH,
        json={"question": "Pixel-Animationen mit lokaler KI?"},
    )
    assert r.status_code == 202, r.text
    started = r.json()
    assert started["role"] == "assistant" and started["status"] == "running"
    assert "Recherche gestartet" in started["content"] or "Research started" in started["content"]
    # the research is listed in the application, too
    runs = client.get("/api/research/runs", headers=AUTH).json()["runs"]
    assert runs and runs[0]["question"] == "Pixel-Animationen mit lokaler KI?"
    gone = client.post(
        "/api/conversations/00000000-0000-0000-0000-000000000000/research",
        headers=AUTH,
        json={"question": "Was gibt es?"},
    )
    assert gone.status_code == 404
