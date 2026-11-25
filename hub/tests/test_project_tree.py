"""Tree rules of the Project application (PPH-01a). Pure: no database needed."""

import pytest

from topcoat_hub.project_tree import TreeError, check_parent


def test_no_parent_is_always_fine() -> None:
    check_parent("a", None, {"a": None})


def test_parent_in_the_same_project_is_fine() -> None:
    check_parent("b", "a", {"a": None, "b": "a"})


def test_parent_outside_the_project_is_refused() -> None:
    with pytest.raises(TreeError, match="not in this project"):
        check_parent("a", "elsewhere", {"a": None})


def test_a_task_cannot_be_its_own_parent() -> None:
    with pytest.raises(TreeError, match="below itself"):
        check_parent("a", "a", {"a": "a"})


def test_a_loop_in_the_parent_chain_is_refused() -> None:
    # a is the parent of b, b the parent of c; making c the parent of a closes the loop
    parents = {"a": "c", "b": "a", "c": "b"}
    with pytest.raises(TreeError, match="below itself"):
        check_parent("a", "c", parents)


def test_moving_a_task_below_its_own_child_is_refused() -> None:
    parents = {"a": None, "b": "a", "c": "b"}
    with pytest.raises(TreeError, match="below itself"):
        check_parent("a", "c", parents)
