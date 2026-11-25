"""Test runs that go on in the node (repo.test_start / repo.test_status) as the agent uses them:
starting returns at once, asking after a running test is free and waits inside one step, and an
answer that says "tests passed" is checked against what the task saw - by the hub itself, and for
the tools named in ``review_before_answer_tools`` by a second model."""

import dataclasses
from typing import Any

from fastapi.testclient import TestClient

from tests import test_dev_workshop as workshop
from tests.test_agent import finish, start, tool, use, wait_for
from tests.test_dev_workshop import dev_node
from topcoat_hub import agent_tests
from topcoat_hub.agent_service import without_blocking_test

repos = workshop.repos  # the fixture, registered here as well
JOB = "0123456789abcdef01234567"


def status(state: str, **more: Any) -> dict[str, Any]:
    return {"repo": "app", "job_id": JOB, "state": state, "running": state == "running", **more}


def node_with_run(client: TestClient, connect, repos, states: list[dict[str, Any]]):
    repos("app", "acme/app", "test-branch", "pytest")
    node_id, node = dev_node(client, connect, "dev-node")
    node.capability_answers["repo.test_start"] = status("running")
    seq = list(states)
    node.capability_answers["repo.test_status"] = lambda _in: seq.pop(0) if len(seq) > 1 else seq[0]
    return node_id, node


ASK = "run the tests in the repo app"


def test_the_agent_starts_a_run_and_asks_for_it_without_spending_calls(
    client: TestClient, connect, repos
) -> None:
    _, node = node_with_run(
        client,
        connect,
        repos,
        [status("running"), status("running"), status("passed", log="1556 passed, 6 skipped")],
    )
    use(
        client,
        tool("dev-node", "repo.test_start", repo="app"),
        tool("dev-node", "repo.test_status", repo="app", job_id=JOB),
        tool("dev-node", "repo.test_status", repo="app", job_id=JOB),  # the same call again
        finish(
            "1556 passed, 6 skipped.",
            [{"claim": "the tests pass: 1556 passed", "tag": "live", "evidence": ["E2"]}],
        ),
        max_tool_calls=2,  # the start counts, asking for it does not
    )
    t = wait_for(client, start(client, ASK).json()["id"], "done")
    assert t["tool_calls"] == 3 and t["stop_reason"] is None, t
    caps = [c["capability"] for c in node.capability_calls]
    # the hub asked again inside one step while the run was going: 1 start + 3 asks (running,
    # running, passed) answered the first status call, one more answered the second
    assert caps[0] == "repo.test_start" and caps.count("repo.test_status") >= 3, caps
    assert all(c["input"].get("wait_seconds", 0) >= 10 for c in node.capability_calls[1:])
    assert "not backed" not in " ".join(t["diagnosis"]["notes"])


def test_an_answer_that_says_passed_while_the_run_is_going_is_sent_back_once(
    client: TestClient, connect, repos
) -> None:
    node_with_run(client, connect, repos, [status("running")])
    model = use(
        client,
        tool("dev-node", "repo.test_start", repo="app"),
        finish("All tests passed.", [{"claim": "tests pass", "tag": "live", "evidence": ["E1"]}]),
        finish("The run is still going; there is no result yet.", []),
        test_wait_seconds=0,
    )
    t = wait_for(client, start(client, ASK).json()["id"], "done")
    assert t["summary"].startswith("The run is still going")
    sent_back = model.calls[2][-1]["content"]
    assert "no repo.test_status in this task showed the state passed" in sent_back


def test_a_run_that_was_ended_is_no_result_and_a_stubborn_answer_gets_a_remark(
    client: TestClient, connect, repos
) -> None:
    node_with_run(client, connect, repos, [status("timed_out_idle", log="..")])
    use(
        client,
        tool("dev-node", "repo.test_start", repo="app"),
        tool("dev-node", "repo.test_status", repo="app", job_id=JOB),
        finish("Tests passed.", []),
        finish("Tests passed, really.", []),
    )
    t = wait_for(client, start(client, ASK).json()["id"], "done")
    notes = " ".join(t["diagnosis"]["notes"])
    assert "not backed by a test run" in notes, notes


def test_the_numbers_of_an_answer_must_be_the_numbers_of_the_run(
    client: TestClient, connect, repos
) -> None:
    node_with_run(client, connect, repos, [status("passed", log="3 passed in 1.0s")])
    model = use(
        client,
        tool("dev-node", "repo.test_start", repo="app"),
        tool("dev-node", "repo.test_status", repo="app", job_id=JOB),
        finish("1556 passed.", []),
        finish("3 passed.", []),
    )
    t = wait_for(client, start(client, ASK).json()["id"], "done")
    assert t["summary"] == "3 passed."
    assert "does not show that number" in model.calls[3][-1]["content"]


class _Check:
    """A stand-in for the second model."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.seen: list[dict[str, Any]] = []

    def wanted(self, used) -> bool:
        return "repo.test_start" in used

    async def check(self, prompt, summary, findings, evidence, author_model):
        self.seen.append({"prompt": prompt, "summary": summary, "evidence": evidence})
        return self.answers.pop(0)


def _checked(client: TestClient, check: _Check, *replies: Any) -> dict[str, Any]:
    client.app.state.agents.loop.precheck = check
    use(client, *replies)
    return wait_for(client, start(client, ASK).json()["id"], "done")


def test_a_second_model_can_send_the_draft_back_once(client: TestClient, connect, repos) -> None:
    node_with_run(client, connect, repos, [status("passed", log="3 passed")])
    check = _Check(
        [{"claim": "all green", "reason": "the log shows 3 passed", "severity": "major"}], []
    )
    t = _checked(
        client,
        check,
        tool("dev-node", "repo.test_start", repo="app"),
        tool("dev-node", "repo.test_status", repo="app", job_id=JOB),
        finish("3 passed, everything is green.", []),
        finish("3 passed.", []),
    )
    assert t["summary"] == "3 passed." and len(check.seen) == 1  # asked once only
    assert any(e[1] == "repo.test_status" for e in check.seen[0]["evidence"])


def test_a_second_model_that_is_not_there_is_a_remark_not_a_stop(
    client: TestClient, connect, repos
) -> None:
    node_with_run(client, connect, repos, [status("passed", log="3 passed")])
    t = _checked(
        client,
        _Check(None),
        tool("dev-node", "repo.test_start", repo="app"),
        tool("dev-node", "repo.test_status", repo="app", job_id=JOB),
        finish("3 passed.", []),
    )
    assert t["summary"] == "3 passed."
    assert "not cross-checked by a second model" in " ".join(t["diagnosis"]["notes"])


def test_minor_findings_of_the_second_model_become_remarks(
    client: TestClient, connect, repos
) -> None:
    node_with_run(client, connect, repos, [status("passed", log="3 passed")])
    t = _checked(
        client,
        _Check([{"claim": "fast", "reason": "no timing shown", "severity": "minor"}]),
        tool("dev-node", "repo.test_start", repo="app"),
        tool("dev-node", "repo.test_status", repo="app", job_id=JOB),
        finish("3 passed, fast.", []),
    )
    assert any(n.startswith("second model: fast") for n in t["diagnosis"]["notes"])


# --- the pure parts -------------------------------------------------------------------------


def test_what_counts_as_a_claim_of_passing_tests() -> None:
    for yes in (
        "All tests passed.",
        "1556 passed, 6 skipped",
        "Alle Tests bestanden",
        "pytest is green",
    ):
        assert agent_tests.claims_success(yes), yes
    for no in (
        "The tests did not pass.",
        "kein Test bestanden",
        "The run is still going",
        "Tests are failing",
    ):
        assert not agent_tests.claims_success(no), no


def test_a_claim_is_backed_only_by_a_passed_run_with_the_same_numbers() -> None:
    runs = agent_tests.TestRuns()
    assert agent_tests.unbacked_claim(["Nothing about tests."], runs) == ""
    assert "no test run was seen" in agent_tests.unbacked_claim(["All tests passed."], runs)
    runs.note("repo.test_start", {"repo": "app", "job_id": JOB})
    assert "never asked for" in agent_tests.unbacked_claim(["All tests passed."], runs)
    runs.note("repo.test_status", status("running"))
    assert "last seen as running" in agent_tests.unbacked_claim(["All tests passed."], runs)
    runs.note("repo.test_status", status("passed", log="12 passed in 3s"))
    assert agent_tests.unbacked_claim(["All tests passed."], runs) == ""
    assert agent_tests.unbacked_claim(["12 passed"], runs) == ""
    assert "does not show that number" in agent_tests.unbacked_claim(["13 passed"], runs)


def test_the_blocking_test_is_not_offered_where_a_background_run_is() -> None:
    def t(node: str, cap: str) -> dict[str, Any]:
        return {"node": node, "capability": cap}

    tools = [
        t("a", "repo.test"),
        t("a", "repo.test_start"),
        t("b", "repo.test"),
        t("a", "repo.read"),
    ]
    left = [(x["node"], x["capability"]) for x in without_blocking_test(tools)]
    assert left == [("a", "repo.test_start"), ("b", "repo.test"), ("a", "repo.read")]


def test_a_second_model_is_asked_only_for_the_tools_named() -> None:
    from topcoat_hub.agent_precheck import parse, tool_set

    assert tool_set(" repo.test_start, repo.apply_patch ,") == {
        "repo.test_start",
        "repo.apply_patch",
    }
    assert tool_set("") == frozenset()
    found = parse(
        '{"findings": [{"claim": "x passed", "reason": "r", "evidence": "E1", "severity": "major"},'
        ' {"claim": "y", "reason": "r", "evidence": "E9", "severity": "bogus"}]}',
        {"E1"},
    )
    assert [(f["evidence"], f["severity"]) for f in found] == [("E1", "major"), (None, "minor")]
    assert parse("not json", {"E1"}) == []


def test_the_limits_have_a_wait_for_tests() -> None:
    from topcoat_hub.agent_loop import Limits

    assert dataclasses.replace(Limits(), test_wait_seconds=1).test_wait_seconds == 1


def test_the_second_model_gets_every_failed_test_and_the_summary_of_a_long_log() -> None:
    from topcoat_hub.agent_precheck import condense

    traceback = "\n".join(f"  File x{i}.py, line {i}" for i in range(80))
    log = (
        "state=failed  exit_code=1  passed=False\n"
        + traceback
        + "\n=== short test summary info ===\n"
        + "\n".join(
            f"FAILED tests/test_a.py::test_{n} - AssertionError: {'x' * 300}" for n in range(3)
        )
        + "\n=== 3 failed, 1482 passed, 6 skipped in 169.58s ===\n"
    )
    out = condense(log)
    assert out.startswith("state=failed")
    assert out.count("FAILED tests/test_a.py") == 3
    assert "3 failed, 1482 passed" in out
    assert len(out) <= 1000
    assert condense("plain output\nno verdict here") == "plain output\nno verdict here"


def test_naming_the_passed_count_of_a_failed_run_is_not_claiming_success() -> None:
    runs = agent_tests.TestRuns()
    runs.note(
        agent_tests.STATUS,
        {
            "job_id": "j1",
            "repo": "r",
            "state": "failed",
            "log": "FAILED tests/test_x.py::test_a\n=== 1 failed, 1484 passed, 6 skipped ===",
        },
    )
    said = "Ein Test ist fehlgeschlagen. 1484 Tests waren erfolgreich, 6 wurden übersprungen."
    assert agent_tests.unbacked_claim([said], runs) == ""
    assert agent_tests.unbacked_claim(["1484 passed"], runs) == ""
    # a wrong number, or a plain "all passed", is still not backed by a failed run
    assert agent_tests.unbacked_claim(["1500 Tests waren erfolgreich"], runs) != ""
    assert agent_tests.unbacked_claim(["Alle Tests bestanden. 1484 passed"], runs) != ""


def test_a_program_can_switch_the_claim_check_off_and_the_answer_goes_out_at_once(
    client: TestClient, connect, repos
) -> None:
    from topcoat_hub.auth import CurrentUser
    from topcoat_hub.models import DEV_ADMIN_ID

    node_with_run(client, connect, repos, [status("running")])
    model = use(
        client,
        tool("dev-node", "repo.test_start", repo="app"),
        finish("All tests passed.", [{"claim": "tests pass", "tag": "live", "evidence": ["E1"]}]),
        finish("never asked for", []),
        test_wait_seconds=0,
    )
    agents = client.app.state.agents
    admin = CurrentUser(id=str(DEV_ADMIN_ID), name="admin", role="admin")

    async def go() -> str:
        return str((await agents.start(admin, ASK, None, hooks={"claims": False})).id)

    t = wait_for(client, client.portal.call(go), "done")
    assert t["summary"].startswith("All tests passed")  # not sent back, unlike the default
    assert len(model.calls) == 2
