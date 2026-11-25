"""A finish is not finished while it promises a step it did not take, and it names only a work
branch the task saw (no model: the hub's own checks, ``agent_done``)."""

from fastapi.testclient import TestClient

from tests import test_dev_workshop as workshop
from tests.test_agent import finish, start, tool, use, wait_for
from tests.test_dev_workshop import dev_node
from topcoat_hub import agent_done

repos = workshop.repos  # the fixture, registered here as well
ASK = "look at the repo app and tell me which branch the work is on"


def test_a_promise_in_the_first_person_is_found_advice_and_denials_are_not() -> None:
    assert "check the logs" in agent_done.announced(["Done. I will now check the logs."])
    assert agent_done.announced(["Ich werde jetzt die Datei prüfen."])
    assert agent_done.announced(["Ich prüfe das gleich noch."])
    assert agent_done.announced(["Let me run the tests."])
    assert agent_done.announced(["Als nächstes schaue ich mir den Branch an."])
    # advice to the user, a denial, a report of what was done: no promise
    assert agent_done.announced(["You could run the tests next."]) == ""
    assert agent_done.announced(["Ich werde das nicht prüfen, es fehlt das Werkzeug."]) == ""
    assert agent_done.announced(["I checked the logs and found one error."]) == ""
    assert agent_done.announced(["Ich habe den Branch geprüft: topcoat/x."]) == ""
    assert agent_done.announced(["", "nothing here"]) == ""


def test_a_branch_must_have_been_shown_by_a_repo_result_or_named_in_the_question() -> None:
    seen = agent_done.Branches()
    seen.of_prompt("push topcoat/from-the-question later")
    seen.note("repo.status", {"repo": "app", "branch": "topcoat/shown-by-status"})
    seen.note("repo.start", {"repo": "app", "branch": "topcoat/started", "based_on": "origin/main"})
    seen.note("web.fetch", {"branch": "topcoat/web-is-no-repo-result"})
    assert agent_done.unbacked_branch(["Branch topcoat/shown-by-status, done."], seen) == ""
    assert agent_done.unbacked_branch(["Pushed topcoat/from-the-question."], seen) == ""
    assert agent_done.unbacked_branch(["No branch to report."], seen) == ""
    said = agent_done.unbacked_branch(["On topcoat/push-test and test/other."], seen)
    assert "topcoat/push-test" in said and "topcoat/shown-by-status" in said
    assert "topcoat/web-is-no-repo-result" in agent_done.unbacked_branch(
        ["topcoat/web-is-no-repo-result"], seen
    )


def workshop_node(client: TestClient, connect, repos, branch: str = "topcoat/real-work"):
    repos("app", "acme/app", "test-branch", "pytest")
    _, node = dev_node(client, connect, "dev-node")
    node.capability_answers["repo.sync"] = {"repo": "app", "branch": branch, "head": "abc123"}
    return node


def test_an_invented_branch_goes_back_once_and_the_real_name_is_delivered(
    client: TestClient, connect, repos
) -> None:
    workshop_node(client, connect, repos)
    model = use(
        client,
        tool("dev-node", "repo.sync", repo="app"),
        finish("The work is on topcoat/push-test.", []),
        finish("The work is on topcoat/real-work.", []),
    )
    t = wait_for(client, start(client, ASK).json()["id"], "done")
    assert t["summary"] == "The work is on topcoat/real-work."
    sent_back = model.calls[2][-1]["content"]
    assert "topcoat/push-test" in sent_back and "topcoat/real-work" in sent_back


def test_a_stubborn_invented_branch_is_delivered_with_a_remark(
    client: TestClient, connect, repos
) -> None:
    workshop_node(client, connect, repos)
    use(
        client,
        tool("dev-node", "repo.sync", repo="app"),
        finish("On topcoat/push-test.", []),
        finish("Still topcoat/push-test.", []),
    )
    t = wait_for(client, start(client, ASK).json()["id"], "done")
    assert "branch named in the answer was not shown" in " ".join(t["diagnosis"]["notes"])


def test_a_finish_that_announces_a_step_goes_back_once(client: TestClient, connect, repos) -> None:
    workshop_node(client, connect, repos)
    model = use(
        client,
        tool("dev-node", "repo.sync", repo="app"),
        finish("Ich werde jetzt noch die Datei prüfen.", []),
        tool("dev-node", "repo.sync", repo="app", note="again"),
        finish("Die Arbeit liegt auf topcoat/real-work, geändert ist hallo.py.", []),
    )
    t = wait_for(client, start(client, ASK).json()["id"], "done")
    assert t["summary"].startswith("Die Arbeit liegt auf")
    assert "announces a step it did not take" in model.calls[2][-1]["content"]


def test_a_second_promise_is_delivered_with_a_remark_not_looped(
    client: TestClient, connect, repos
) -> None:
    workshop_node(client, connect, repos)
    use(
        client,
        tool("dev-node", "repo.sync", repo="app"),
        finish("I will now check the logs.", []),
        finish("I will check the logs next.", []),
    )
    t = wait_for(client, start(client, ASK).json()["id"], "done")
    assert "announces a step that was not taken" in " ".join(t["diagnosis"]["notes"])


def test_the_checks_do_not_touch_a_finish_that_reports_what_happened(
    client: TestClient, connect, repos
) -> None:
    workshop_node(client, connect, repos)
    use(
        client,
        tool("dev-node", "repo.sync", repo="app"),
        finish("Branch topcoat/real-work, hallo.py changed.", []),
    )
    t = wait_for(client, start(client, ASK).json()["id"], "done")
    assert t["summary"].startswith("Branch topcoat/real-work")
    assert t["diagnosis"]["notes"] == [] or "branch" not in " ".join(t["diagnosis"]["notes"])
