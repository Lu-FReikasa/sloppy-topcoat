"""The work the hub does on its own (ARCHITECTURE D75): every background task has a trigger and its
steps, and the values come from the hub's settings. (The flows an admin could copy for this were
taken out; none was ever in use.)"""

import uuid

from fastapi.testclient import TestClient

from topcoat_hub import background
from topcoat_hub.config import Settings
from topcoat_hub.models import DEV_ADMIN_ID


def test_every_task_says_what_the_settings_say() -> None:
    settings = Settings(chat_digest_idle_seconds=1200, review_batch=7, memory_care_hour=4)
    chat = background.defaults("chat_end", settings)
    assert chat.idle_minutes == 20.0 and chat.on is True
    assert chat.limit("review", 99) == 7
    assert background.defaults("care", settings).hour == 4
    assert set(background.TASKS) >= {"chat_end", "consolidation", "care", "knowledge"}
    assert "recheck" not in background.TASKS  # D111
    for task in background.TASKS:
        assert background.defaults(task, settings).task == task


def test_a_step_with_no_number_takes_the_one_the_caller_names() -> None:
    values = background.defaults("care", Settings())
    assert values.limit("no-such-step", 10) == 10
    assert values.step("no-such-step").on is True  # an unknown step is "on", with the hub's rule


def test_switched_off_work_does_not_start(client: TestClient) -> None:
    settings = client.app.state.settings
    settings.chat_digest_enabled = False
    settings.memory_consolidation_interval_seconds = 0
    digester = client.app.state.digester
    digester.after_answer(DEV_ADMIN_ID, uuid.uuid4())
    assert digester._pending == {}

    async def ticks() -> int:
        return await client.app.state.memory.run_scheduled()

    assert client.portal.call(ticks) == 0


def test_a_service_reads_the_settings_of_its_task(client: TestClient) -> None:
    care = client.app.state.memory_care
    client.app.state.settings.memory_care_hour = 3
    assert background.current(care, "care").hour == 3
