"""The hub guards the shared scratch copy of the dev workshop (``agent_workshop``): no discarding
of uncommitted work on the model's own initiative, a new branch when the question asks for one,
no blind overwrite of a file the task never read. No model is involved in these rules."""

from fastapi.testclient import TestClient

from tests.test_agent import finish, start, tool, use, wait_for
from tests.test_dev_workshop import dev_node, repos  # noqa: F401  (the fixture)
from tests.test_tools import set_config
from topcoat_hub import agent_precheck
from topcoat_hub.agent_workshop import Workshop

NEW_BRANCH = "Im Repo app, auf einem neuen Branch topcoat/x: lege tools/greet.py an"
PLAIN = "Im Repo app: lege tools/greet.py an"
ALL = {"repo.start", "repo.status", "repo.reset", "repo.write", "repo.list"}


def test_the_question_decides_whether_it_wants_a_branch_or_a_discard() -> None:
    w = Workshop()
    w.of_prompt("Auf einem neuen Branch topcoat/x bitte")
    assert w.asks_branch and not w.may_discard
    w.of_prompt("Start on a new branch and discard what is there")
    assert w.asks_branch and w.may_discard
    w.of_prompt("Die Arbeitskopie darf verworfen werden")
    assert w.may_discard and not w.asks_branch
    w.of_prompt("Lege eine Datei an")
    assert not w.asks_branch and not w.may_discard


def test_work_in_the_copy_is_not_thrown_away_on_the_models_own_initiative() -> None:
    w = Workshop()
    w.of_prompt(PLAIN)
    w.note("repo.status", {"repo": "app"}, {"clean": False, "changed": ["M a.py", "?? docs/"]})
    for call in (("repo.reset", {"repo": "app"}), ("repo.start", {"repo": "app", "discard": True})):
        said = w.refusal(*call, ALL)
        assert "a.py" in said and "ask whether it may be thrown away" in said
    assert w.refusal("repo.start", {"repo": "app", "topic": "x"}, ALL) == ""  # the node refuses
    w.note("repo.reset", {"repo": "app"}, {"clean": True})
    assert w.refusal("repo.reset", {"repo": "app"}, ALL) == ""


def test_an_unknown_state_is_looked_at_before_anything_is_discarded() -> None:
    w = Workshop()
    w.of_prompt(PLAIN)
    assert "repo.status" in w.refusal("repo.reset", {"repo": "app"}, ALL)
    assert w.refusal("repo.reset", {"repo": "app"}, {"repo.reset"}) == ""  # nothing to look with
    w.note("repo.write", {"repo": "app", "path": "a.py"}, {"bytes": 3})
    assert "uncommitted work" in w.refusal("repo.reset", {"repo": "app"}, ALL)


def test_a_question_that_asks_for_it_may_discard() -> None:
    w = Workshop()
    w.of_prompt("Im Repo app, die Arbeitskopie darf verworfen werden: starte topcoat/x")
    w.note("repo.status", {"repo": "app"}, {"clean": False, "changed": ["M a.py"]})
    assert w.refusal("repo.start", {"repo": "app", "topic": "x", "discard": True}, ALL) == ""


def test_a_new_branch_comes_before_the_first_change() -> None:
    w = Workshop()
    w.of_prompt(NEW_BRANCH)
    write = {"repo": "app", "path": "tools/greet.py", "content": "x"}
    assert "repo.start" in w.refusal("repo.write", write, ALL)
    assert w.refusal("repo.write", write, {"repo.write"}) == ""  # no tool to start one
    w.note("repo.start", {"repo": "app", "topic": "x"}, {"branch": "topcoat/x"})
    assert w.refusal("repo.write", write, ALL) == ""
    other = Workshop()
    other.of_prompt(PLAIN)
    assert other.refusal("repo.write", write, ALL) == ""


def test_an_existing_file_is_read_before_it_is_replaced() -> None:
    w = Workshop()
    w.of_prompt(PLAIN)
    w.note("repo.list", {"repo": "app"}, {"files": ["README.md", "a.py"]})
    readme = {"repo": "app", "path": "README.md", "content": "new"}
    assert "have not read it" in w.refusal("repo.write", readme, ALL)
    assert w.refusal("repo.write", {**readme, "path": "new.py"}, ALL) == ""
    assert w.refusal("repo.edit", {**readme, "old": "a", "new": "b"}, ALL) == ""
    w.note("repo.read", {"repo": "app", "path": "README.md"}, {"text": "old"})
    assert w.refusal("repo.write", readme, ALL) == ""
    w.note("repo.write", {"repo": "app", "path": "a.py"}, {"bytes": 1})  # written here: known
    assert w.refusal("repo.write", {**readme, "path": "a.py"}, ALL) == ""


def test_a_finished_start_forgets_what_the_old_branch_showed() -> None:
    w = Workshop()
    w.of_prompt(PLAIN)
    w.note("repo.list", {"repo": "app"}, {"files": ["README.md"]})
    w.note("repo.start", {"repo": "app", "topic": "x", "discard": True}, {"branch": "topcoat/x"})
    assert w.refusal("repo.write", {"repo": "app", "path": "README.md", "content": "n"}, ALL) == ""


def enable(client: TestClient, node_id: str) -> None:
    set_config(client, node_id, lambda cfg: cfg["capabilities"].extend(sorted(ALL)))


def test_a_refused_reset_never_reaches_the_node_and_the_model_is_told_why(
    client: TestClient,
    connect,
    repos,  # noqa: F811
) -> None:
    repos("app", "acme/app", "test-branch", "pytest")
    node_id, node = dev_node(client, connect, "dev-node")
    enable(client, node_id)
    node.capability_answers["repo.status"] = {"clean": False, "changed": ["M hallo.py"]}
    node.capability_answers["repo.reset"] = {"clean": True}
    model = use(
        client,
        tool("dev-node", "repo.status", repo="app"),
        tool("dev-node", "repo.reset", repo="app"),
        finish("The copy holds unsaved work in hallo.py; it may be thrown away?", []),
    )
    t = wait_for(client, start(client, PLAIN).json()["id"], "done")
    assert t["summary"].startswith("The copy holds unsaved work")
    assert "workshop_rule" in model.calls[2][-1]["content"]
    assert [c["capability"] for c in node.capability_calls] == ["repo.status"]


def test_a_file_change_is_never_a_direct_call_of_the_agent(
    client: TestClient,
    connect,
    repos,  # noqa: F811
) -> None:
    # D99: repo.write is offered only as a proposal; the user confirms each change with a click.
    # A direct call of it never reaches the node, and the branch start still does.
    repos("app", "acme/app", "test-branch", "pytest")
    node_id, node = dev_node(client, connect, "dev-node")
    enable(client, node_id)
    node.capability_answers["repo.write"] = {"bytes": 4}
    node.capability_answers["repo.start"] = {"branch": "topcoat/x", "repo": "app"}
    use(
        client,
        tool("dev-node", "repo.write", repo="app", path="a.py", content="x"),
        tool("dev-node", "repo.start", repo="app", topic="x"),
        finish("Proposed the change; the user confirms it.", []),
    )
    wait_for(client, start(client, NEW_BRANCH).json()["id"], "done")
    assert [c["capability"] for c in node.capability_calls] == ["repo.start"]


def test_the_hint_names_the_registered_repositories_and_the_arguments(
    client: TestClient,
    connect,
    repos,  # noqa: F811
) -> None:
    repos("app", "acme/app", "test-branch", "pytest")
    dev_node(client, connect, "dev-node")
    model = use(client, finish("ok", []))
    wait_for(client, start(client, PLAIN).json()["id"], "done")
    first = model.calls[0][1]["content"]
    assert "registered repositories are: app (test-branch)" in first
    assert "propose each change in the proposals of your finish" in first


def test_a_finding_that_says_it_is_fine_is_no_finding() -> None:
    found = agent_precheck.parse(
        '{"findings": ['
        '{"claim": "a", "reason": "Kein Widerspruch zu E1.", "severity": "minor"},'
        '{"claim": "b", "reason": "There is no contradiction with E2.", "severity": "minor"},'
        '{"claim": "c", "reason": "The statement is supported by E3.", "severity": "minor"},'
        '{"claim": "d", "reason": "No tool result contains this number.", "severity": "major"}]}',
        {"E1"},
    )
    assert [f["claim"] for f in found] == ["d"]
