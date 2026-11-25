from topcoat_hub import tool_groups


def caps(*names: str) -> list[dict[str, str]]:
    return [{"capability": n, "node": "n"} for n in names]


def test_a_docker_question_needs_something_that_knows_containers() -> None:
    q = "Welche Docker-Container laufen auf alpha?"
    assert tool_groups.missing_tools(q, caps("system.info", "disk.usage")) == ["docker"]
    assert tool_groups.missing_tools(q, caps("docker.ps")) == []
    # the inventory and the notes can stand in
    assert tool_groups.missing_tools(q, caps("inventory.list")) == []
    assert tool_groups.missing_tools(q, caps("knowledge.search")) == []


def test_a_question_that_names_no_topic_needs_nothing() -> None:
    assert tool_groups.missing_tools("why is the disk full?", caps("system.info")) == []
    assert tool_groups.missing_tools("why is the disk full?", []) == []


def test_several_missing_topics_are_all_named() -> None:
    q = "docker is slow and journalctl shows errors"
    assert tool_groups.missing_tools(q, caps("system.info")) == ["docker", "journal"]


def test_the_summary_names_the_groups_the_agent_can_use() -> None:
    assert "systemd" in tool_groups.tools_summary(caps("journal.logs"))
    assert tool_groups.tools_summary([]) == "nothing"


def test_a_stand_in_is_not_a_live_tool() -> None:
    q = "Welche Docker-Container laufen?"
    assert tool_groups.no_live_tool(q, caps("inventory.list")) == ["docker"]
    assert tool_groups.no_live_tool(q, caps("docker.ps")) == []
    assert tool_groups.no_live_tool(q, caps("system.info")) == []  # nothing at all: missing_tools


def test_the_read_only_shell_can_look_at_containers_and_services_itself() -> None:
    q = "Läuft der Container nextcloud, und was sagt systemctl is-active?"
    assert tool_groups.missing_tools(q, caps("system.info")) == ["docker", "journal"]
    assert tool_groups.missing_tools(q, caps("shell.read")) == []
    assert tool_groups.no_live_tool(q, caps("shell.read")) == []


def test_a_question_about_repo_files_may_use_the_workshop_unless_it_is_switched_off() -> None:
    ask = "Lege im Repo test eine Datei hello.py an und lass pytest laufen"
    assert tool_groups.may_use_workshop(ask, None)
    assert not tool_groups.may_use_workshop("Wie spät ist es?", None)
    assert not tool_groups.may_use_workshop(ask, {"nodes": None, "groups": ["docker"]})
    assert tool_groups.may_use_workshop(ask, {"nodes": None, "groups": ["workshop"]})
