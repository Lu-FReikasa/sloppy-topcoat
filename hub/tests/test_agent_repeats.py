"""What a run does when the model asks the same thing again (live 2026-11-08/09: a quarter of the
runs ended early). A call that failed is not run again; a result that was cut shows its next part;
a result that left the prompt to save room is shown once more; only a real repeat counts."""

from fastapi.testclient import TestClient

from tests.test_agent import finish, start, task, tool, use, vm, wait_for  # noqa: F401
from topcoat_hub.agent_loop import KeptOutput, refusal_reason


def _observations(model) -> list[str]:
    return [str(call[-1]["content"]) for call in model.calls[1:]]


def test_a_failed_call_asked_again_is_not_run_again(client: TestClient, vm) -> None:  # noqa: F811
    _, node = vm
    node.capability_answers["docker.logs"] = {
        "error": {"code": "invalid_args", "message": "no container named ghost"}
    }
    model = use(
        client,
        tool("vm-docker", "docker.logs", container="ghost", lines=5),
        tool("vm-docker", "docker.logs", container="ghost", lines=5),
        finish("The container does not exist."),
    )
    wait_for(client, start(client).json()["id"], "done")
    assert len(node.capability_calls) == 1  # the second one never reached the node
    second = _observations(model)[1]
    assert "This exact call failed a moment ago (invalid_args: no container named ghost)" in second


def test_a_call_that_timed_out_may_be_tried_again(client: TestClient, vm) -> None:  # noqa: F811
    _, node = vm
    node.capability_answers["docker.logs"] = {"error": {"code": "timeout", "message": "slow"}}
    use(
        client,
        tool("vm-docker", "docker.logs", container="web", lines=5),
        tool("vm-docker", "docker.logs", container="web", lines=5),
        finish(),
    )
    wait_for(client, start(client).json()["id"], "done")
    assert len(node.capability_calls) == 2


def test_a_cut_result_asked_again_shows_its_next_part(client: TestClient, vm) -> None:  # noqa: F811
    _, node = vm
    node.capability_answers["system.info"] = {"blob": "a" * 1200 + "MIDDLE" + "b" * 1200 + "END"}
    model = use(
        client,
        tool("vm-docker", "system.info"),
        tool("vm-docker", "system.info"),
        tool("vm-docker", "system.info"),
        finish("Read it all."),
        observation_chars=500,
    )
    t = wait_for(client, start(client).json()["id"], "done")
    assert len(node.capability_calls) == 1
    first, second, third = _observations(model)[:3]
    assert "the same call again shows the next part" in first and "MIDDLE" not in first
    assert "continued: characters 501-1000" in second and "[E1]" in second
    assert "MIDDLE" in third
    assert t["stop_reason"] in (None, "")  # no repeated_calls


def test_reading_on_in_a_cut_result_is_progress(client: TestClient, vm) -> None:  # noqa: F811
    # live 2026-11-17: a client list in five parts; three parts read and one repeat ended the run
    # as no_progress before the phone was found
    _, node = vm
    node.capability_answers["system.info"] = {"blob": "a" * 1800 + "PHONE" + "b" * 300}
    model = use(
        client,
        tool("vm-docker", "system.info"),
        tool("vm-docker", "system.info"),
        tool("vm-docker", "system.info"),
        tool("vm-docker", "system.info"),
        finish("Found the phone."),
        observation_chars=500,
        max_stalled_turns=3,
    )
    t = wait_for(client, start(client).json()["id"], "done")
    assert t["stop_reason"] in (None, "")
    assert any("PHONE" in o for o in _observations(model))


def test_a_result_dropped_from_the_prompt_is_shown_once_more(client: TestClient, vm) -> None:  # noqa: F811
    _, node = vm
    model = use(
        client,
        tool("vm-docker", "system.info"),
        tool("vm-docker", "docker.logs", container="web", lines=3),
        tool("vm-docker", "docker.logs", container="db", lines=3),
        tool("vm-docker", "system.info"),  # E1 left the prompt (tiny budget): shown again
        tool("vm-docker", "system.info"),  # now it is a real repeat
        finish(),
        context_chars=100,
    )
    tid = start(client).json()["id"]
    wait_for(client, tid, "done")
    infos = [c for c in node.capability_calls if c["capability"] == "system.info"]
    assert len(infos) == 1
    obs = _observations(model)
    assert "shown again" in obs[3] and "[E1]" in obs[3]
    assert "You already made this call" in obs[4]
    kinds = [
        s["data"]["tool"]["code"]
        for s in task(client, tid)["steps"]
        if s["kind"] == "observation" and (s["data"].get("tool") or {}).get("code")
    ]
    assert "shown_again" in kinds and "repeat" in kinds


def test_kept_output_goes_on_forward_and_a_log_goes_back() -> None:
    text = "x" * 1000
    kept = KeptOutput.kept(text, text[:500] + "\n[… cut]", newest=False)
    part, note = kept.next_part(500) or ("", "")
    assert len(part) == 500 and "501-1000 of 1000" in note
    assert kept.next_part(500) is None
    log = "\n".join(f"line {i}" for i in range(400))
    shown = "[… earlier lines left out]\n" + log[-300:].split("\n", 1)[1]
    back = KeptOutput.kept(log, shown, newest=True)
    part, note = back.next_part(500) or ("", "")
    assert "earlier lines" in note and log.endswith(shown.split("\n", 1)[1])
    assert part and log.index(part[:20]) < log.index(shown.split("\n", 1)[1][:20])


def test_the_refusal_names_the_right_reason() -> None:
    offered = [{"node": "vm", "capability": "system.info"}]
    usable = [
        *offered,
        {"node": "vm", "capability": "docker.logs"},
        {"node": "pc", "capability": "windows.events"},
    ]
    assert "not part of this run's scope" in refusal_reason("pc", "windows.events", offered, usable)
    group = refusal_reason("vm", "docker.logs", offered, usable)
    assert group.startswith("you cannot use that") and "tool group" in group
    assert refusal_reason("vm", "docker.restart", offered, usable) == "you cannot use that."


def test_results_that_bring_nothing_new_are_named_and_counted(client: TestClient, vm) -> None:  # noqa: F811
    model = use(
        client,
        tool("vm-docker", "docker.logs", container="web", lines=3),
        tool("vm-docker", "docker.logs", container="web", lines=4),  # the same output again
        tool("vm-docker", "docker.logs", container="web", lines=5),
        tool("vm-docker", "docker.logs", container="web", lines=6),
        finish(),
    )
    wait_for(client, start(client).json()["id"], "done")
    obs = _observations(model)
    assert "the same result as [E1]" in obs[1] and "nothing new" not in obs[1]
    assert (
        "the same result as [E1]" in obs[3]
        and "Your last three calls brought nothing new" in obs[3]
    )


def test_an_empty_output_says_nothing() -> None:
    from topcoat_hub.agent_loop import says_nothing

    assert says_nothing({"data": [], "masked": 0, "query": "windows.events"})
    assert says_nothing({"query": "x", "hits": [], "count": 0})
    assert not says_nothing({"data": [{"Id": 225}]})
    assert not says_nothing({"hostname": "vm"})


def test_a_tool_name_written_as_the_action_is_read_as_the_call(client: TestClient, vm) -> None:  # noqa: F811
    # live 2026-11-10: {"action": "project.list"} three times ended four project runs as bad_format
    import json as _json

    _, node = vm
    use(
        client,
        _json.dumps({"action": "system.info", "reason": "look"}),
        _json.dumps({"action": "docker.logs", "container": "web", "lines": 5}),
        finish(),
    )
    t = wait_for(client, start(client).json()["id"], "done")
    assert [c["capability"] for c in node.capability_calls] == ["system.info", "docker.logs"]
    assert node.capability_calls[1]["input"] == {"container": "web", "lines": 5}
    assert t["stop_reason"] in (None, "")


def test_an_action_that_names_no_tool_is_still_refused() -> None:
    import pytest

    from topcoat_hub.agent_prompts import ActionError, action_from_obj

    with pytest.raises(ActionError):
        action_from_obj({"action": "think"})
    call = action_from_obj({"action": "project.list"})
    assert call.capability == "project.list" and call.node == "" and call.input == {}


def test_reading_past_the_end_of_a_file_is_the_end_not_a_failure(client: TestClient, vm) -> None:  # noqa: F811
    _, node = vm
    node.capability_answers["docker.logs"] = {
        "error": {"code": "invalid_args", "message": "docs/THEMES.md has only 67 lines"}
    }
    model = use(
        client,
        tool("vm-docker", "docker.logs", container="web", lines=100),
        finish("Read all of it."),
    )
    t = wait_for(client, start(client).json()["id"], "done")
    seen = _observations(model)[0]
    assert "end of file: docs/THEMES.md has 67 lines" in seen
    assert not (t["diagnosis"] or {}).get("failed_calls")


def test_a_table_nobody_asked_for_is_left_out() -> None:
    from topcoat_hub.agent_prompts import asks_for_table

    assert asks_for_table("Ziel: x\nErgebnisform: Aussage | Beleg")
    assert asks_for_table('When you are done, finish with a table: the field "table" holds')
    assert not asks_for_table("Was läuft auf vm-b?")


def test_empty_results_are_no_progress(client: TestClient, vm) -> None:  # noqa: F811
    # live 2026-11-20: a Development check searched for function names that do not exist, each
    # time a little differently; every empty search counted as progress and the run went on for
    # 49 steps
    _, node = vm
    node.capability_answers["docker.logs"] = {"lines": []}
    use(
        client,
        tool("vm-docker", "docker.logs", container="a", lines=5),
        tool("vm-docker", "docker.logs", container="b", lines=5),
        tool("vm-docker", "docker.logs", container="c", lines=5),
        tool("vm-docker", "docker.logs", container="d", lines=5),
        finish("Nothing found in the logs."),
        max_stalled_turns=3,
    )
    t = wait_for(client, start(client).json()["id"], "done")
    assert t["stop_reason"] == "no_progress"
    assert len(node.capability_calls) < 4
