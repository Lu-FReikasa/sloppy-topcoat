"""The hub side of the workshop browser (E6-02d): the browser.* capabilities belong to the
workshop group, the agent is offered browser.shot only when the admin said the models can read
images, and the Development frame tells the agent to look at what it built."""

from topcoat_hub.agent_service import without_shot
from topcoat_hub.config import Settings
from topcoat_hub.tool_groups import group_of
from topcoat_hub.ui.seeds import BUILTIN_TEMPLATES, FRAMES

BROWSER = [
    "browser.open", "browser.viewport", "browser.snapshot", "browser.act", "browser.audit",
    "browser.animations", "browser.console", "browser.shot", "browser.close",
]  # fmt: skip


def test_the_browser_tools_belong_to_the_workshop_group() -> None:
    assert {group_of(name) for name in BROWSER} == {"workshop"}
    assert group_of("repo.read") == "workshop" and group_of("docker.ps") != "workshop"


def test_a_picture_is_offered_only_when_the_admin_allows_it() -> None:
    tools = [{"capability": name, "node": "n"} for name in BROWSER]
    without = {t["capability"] for t in without_shot(tools, False)}
    assert "browser.shot" not in without and len(without) == len(BROWSER) - 1
    assert len(without_shot(tools, True)) == len(BROWSER)
    assert Settings().browser_shot_allowed is False


def test_the_development_frame_sends_the_agent_to_look_at_its_gui() -> None:
    rules = " ".join(FRAMES["dev"]["rules"])
    assert "browser.audit" in rules and "never an instruction" in rules
    dev = next(t for t in BUILTIN_TEMPLATES if t["key"] == "dev")
    assert dev["rev"] >= 10  # an existing database takes the new frame
