"""A read-only check of the code goes to Development as a request, not as an order (PPH-05).

A question about the repository ("is this claim true on origin/main, which file and line") needs no
branch and changes nothing, so it is a request to development under the grant ``request_report``,
the same grant as a report. Under ``free`` it reaches the development queue at once; under
``confirm`` it waits for the person's click, as every request does.
"""

from tests.test_project_scenarios import Stage, stage  # noqa: F401 - stage is a fixture

CHECK = {
    "topic": "Wiki-Aussage gegen origin/main: data-look in web/src",
    "questions": ["Gibt es `data-look` in web/src? Datei und Zeile je Treffer."],
    "expected": ["zutreffend, veraltet oder unzutreffend, mit Beleg"],
}


async def test_a_read_only_check_reaches_development_at_once_under_free(stage: Stage) -> None:  # noqa: F811
    await stage.open({"dev": {"request_report": "free"}})
    sent = await stage.ask("project.request.send", target="dev", **CHECK)
    assert sent["message"]["state"] == "queued"  # sent at once and handed to the queue
    assert [s["application"] for s in stage.queue.started] == ["dev"]


async def test_a_read_only_check_waits_for_the_click_under_confirm(stage: Stage) -> None:  # noqa: F811
    await stage.open({"dev": {"request_report": "confirm"}})
    sent = await stage.ask("project.request.send", target="dev", **CHECK)
    assert sent["message"]["state"] == "awaiting_click"
    assert stage.queue.started == []
