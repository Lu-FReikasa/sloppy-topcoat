"""The grants of a Projekte project (topcoat_hub.project_scope): what the project agent may do
in one project. Unit tests, no database."""

import pytest

from topcoat_hub import project_scope as scope


def test_a_grant_is_free_or_confirm_and_nothing_else() -> None:
    granted = scope.normalize({"dev": {"request_report": "free", "create_branch": "confirm"}})
    assert granted == {"dev": {"request_report": "free", "create_branch": "confirm"}}
    with pytest.raises(scope.ScopeError):
        scope.normalize({"dev": {"request_report": "always"}})


@pytest.mark.parametrize("action", ["push", "merge", "apply_update", "deploy", "delete"])
def test_the_never_actions_cannot_be_granted(action: str) -> None:
    with pytest.raises(scope.ScopeError):
        scope.normalize({"dev": {action: "confirm"}})


def test_an_unknown_target_or_action_is_refused() -> None:
    with pytest.raises(scope.ScopeError):
        scope.normalize({"homelab": {"request_report": "free"}})
    with pytest.raises(scope.ScopeError):
        scope.normalize({"research": {"create_branch": "free"}})


def test_empty_targets_are_dropped_and_non_objects_refused() -> None:
    assert scope.normalize({"research": {}, "dev": {}}) == {}
    assert scope.normalize({}) == {}
    with pytest.raises(scope.ScopeError):
        scope.normalize(["dev"])
    with pytest.raises(scope.ScopeError):
        scope.normalize({"dev": "free"})


def test_allowed_gives_the_level_or_none() -> None:
    granted = {"dev": {"request_report": "free", "create_branch": "confirm"}}
    assert scope.allowed(granted, "dev", "request_report") == "free"
    assert scope.allowed(granted, "dev", "create_branch") == "confirm"
    assert scope.allowed(granted, "dev", "create_prototype") is None
    assert scope.allowed(granted, "admin", "read_released") is None
    assert scope.allowed(None, "dev", "request_report") is None


def test_allowed_never_opens_a_never_action() -> None:
    assert scope.allowed({"dev": {"push": "free"}}, "dev", "push") is None
