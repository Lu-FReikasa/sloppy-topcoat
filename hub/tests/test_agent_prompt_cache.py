"""The prompt of an agent run only grows at its end, so llama-server can reuse the KV cache of the
slot (it only reuses a byte-identical start of the prompt). A clock read at every step changed the
second message each minute and made the server compute the whole transcript again."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from tests.test_agent import finish, start, tool, use, vm, wait_for  # noqa: F401
from topcoat_hub import agent_loop


def test_the_start_of_the_prompt_stays_the_same_while_a_run_takes_minutes(
    client: TestClient,
    vm,  # noqa: F811 - the fixture of test_agent
    monkeypatch,
) -> None:
    ticks = iter(
        datetime(2026, 11, 4, 9, 0, tzinfo=UTC) + timedelta(minutes=i) for i in range(1000)
    )
    monkeypatch.setattr(agent_loop, "utcnow", lambda: next(ticks))
    model = use(
        client,
        tool("vm-docker", "docker.ps"),
        tool("vm-docker", "system.info"),
        finish(),
    )
    r = start(client)
    assert r.status_code == 201, r.text
    wait_for(client, r.json()["id"], "done")

    asked = [c for c in model.calls if len(c) >= 2]
    assert len(asked) >= 3
    first = asked[0][:2]
    for later in asked[1:]:
        assert later[:2] == first  # system prompt and task message, byte for byte
        assert len(later) > len(first)
