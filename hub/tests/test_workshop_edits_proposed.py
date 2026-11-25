"""D99: a change to a registered repository is never a direct call of the agent. It comes back as a
proposal, and the user confirms each one with a click (the same door as every other change).

The repository tools of a dev node are offered when a question names the repository, and the file
changes are only proposed."""

import uuid

from topcoat_hub import agent_prompts as p
from topcoat_hub import tool_groups
from topcoat_hub.agent_workshop import CHANGES
from topcoat_hub.tools import EXECUTE, Caller, writable


def caps(*names: str) -> list[dict[str, str]]:
    return [{"capability": n, "node": "dev-node"} for n in names]


def test_a_question_about_the_repository_selects_the_workshop_tools() -> None:
    ask = "Schau dir das Repo sloppy-topcoat an und sag mir, was drin steht"
    assert "workshop" in tool_groups.topic_groups(ask)
    _scope, keys = tool_groups.auto_scope(
        ask, None, caps("repo.list", "repo.read", "github.read", "docker.ps")
    )
    assert "workshop" in keys and "github" in keys
    assert "docker" not in keys  # the question names no container


def test_the_workshop_group_is_only_added_when_the_workshop_is_there() -> None:
    ask = "Schau dir das Repo sloppy-topcoat an"
    _, keys = tool_groups.auto_scope(ask, None, caps("github.read", "docker.ps"))
    assert "workshop" not in keys


def test_the_file_changes_are_the_ones_the_agent_may_only_propose() -> None:
    assert frozenset({"repo.write", "repo.edit", "repo.apply_patch"}) == CHANGES


def test_an_admin_may_confirm_a_scratch_change_as_a_proposal() -> None:
    admin = Caller(user_id=uuid.uuid4(), admin=True, via="gui")
    config = {"allowed_actions": [EXECUTE], "capabilities": ["repo.write", "repo.read"]}
    inventory = {
        "capabilities": [
            {"name": "repo.write", "read_only": False, "scratch": True, "input_schema": {}},
            {"name": "repo.read", "read_only": True, "input_schema": {}},
        ]
    }
    assert writable(admin, set(), config, inventory) == ["repo.write"]


def test_a_user_without_a_grant_may_not_confirm_a_change() -> None:
    user = Caller(user_id=uuid.uuid4(), admin=False, via="gui")
    config = {"allowed_actions": [EXECUTE], "capabilities": ["repo.write"]}
    inventory = {
        "capabilities": [
            {"name": "repo.write", "read_only": False, "scratch": True, "input_schema": {}}
        ]
    }
    assert writable(user, set(), config, inventory) == []
    assert writable(user, {"repo.write"}, config, inventory) == ["repo.write"]


def test_the_workshop_hint_tells_the_agent_to_propose_changes() -> None:
    hint = p.workshop_hint(
        "Lege im Repo test eine Datei an", [{"capability": "repo.write"}], [("app", "test-branch")]
    )
    assert "propose each change in the proposals of your finish" in hint
    assert "applies it only with one click" in hint
    assert "write files" not in hint
