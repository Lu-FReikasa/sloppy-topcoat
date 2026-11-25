"""The fingerprint of a failed fix (failure_print.py, plan step 1.13): the same failure in two
runs gives the same print although numbers, ids and paths differ; another failure does not."""

from topcoat_hub import failure_print as fp

RUN_A = """\
hub/tests/test_chat_review.py::test_pick F
E       AssertionError: assert 3 == 2
E        +  where 3 = len([...])
=========================== short test summary info ============================
FAILED hub/tests/test_chat_review.py::test_pick[gpt-oss-1] - AssertionError: assert 3 == 2
1 failed, 3541 passed in 102.31s
"""

RUN_B = """\
/work/copy-8f3a9c/hub/tests/test_chat_review.py::test_pick F
E       AssertionError: assert 4 == 2
=========================== short test summary info ============================
FAILED hub/tests/test_chat_review.py::test_pick[gpt-oss-2] - AssertionError: assert 4 == 2
1 failed, 3542 passed in 98.10s
"""

OTHER = """\
E       KeyError: 'family'
FAILED hub/tests/test_chat_review.py::test_pick - KeyError: 'family'
"""


def test_the_same_failure_gives_the_same_print_across_runs() -> None:
    a = fp.fingerprint("the tests ended failed", RUN_A)
    assert a == fp.fingerprint("the tests ended failed", RUN_B)
    assert a.startswith("AssertionError | test_chat_review.py::test_pick | ")
    assert "3" not in a and "hub/tests" not in a


def test_another_failure_gives_another_print() -> None:
    assert fp.fingerprint("x", RUN_A) != fp.fingerprint("x", OTHER)
    assert fp.fingerprint("x", OTHER).startswith("KeyError | ")


def test_a_refused_draft_is_printed_by_its_reason() -> None:
    why = "die Edits passen nicht zum Code: edit 2 (hub/src/topcoat_hub/x.py): the old text at 41"
    again = "die Edits passen nicht zum Code: edit 2 (hub/src/topcoat_hub/x.py): the old text at 57"
    assert fp.fingerprint(why) == fp.fingerprint(again)
    assert fp.fingerprint(why) != fp.fingerprint("the draft has no JSON object")


def test_web_failures_are_printed_too() -> None:
    log = "src/project/Panel.tsx(12,3): error TS2304: Cannot find name 'foo'.\n"
    assert fp.fingerprint("x", log).startswith("TS2304 | ")
    vitest = " FAIL  src/project/Panel.test.tsx > panel > shows the count\nAssertionError: x\n"
    assert "Panel.test.tsx > panel > shows the count" in fp.fingerprint("x", vitest)


def test_eslint_and_unknown_logs_are_printed_by_their_error_line() -> None:
    """Live 2026-11-25: a red web lint printed only "the tests ended failed", which would make two
    different red runs look the same."""
    lint = (
        "3734 passed, 6 skipped in 483.74s\n/w/web/src/project/ProjectPage.tsx\n"
        "  425:5  error  Error: Calling setState synchronously within an effect  "
        "react-hooks/set-state-in-effect\n✖ 1 problem (1 error, 0 warnings)\n"
    )
    a = fp.fingerprint("the tests ended failed", lint)
    assert a.startswith("react-hooks/set-state-in-effect |") and "setState" in a
    other = "  12:1  error  'x' is defined but never used  no-unused-vars\n"
    assert fp.fingerprint("the tests ended failed", other) != a
    odd = "step 3\nbuild error: module not found\n"
    assert "module not found" in fp.fingerprint("the tests ended failed", odd)


def test_repeats_counts_the_newest_equal_prints() -> None:
    assert fp.repeats([]) == 0
    assert fp.repeats(["a"]) == 1
    assert fp.repeats(["a", "a", "b", "a"]) == 2


def test_the_spot_is_the_files_the_failure_names() -> None:
    paths = ["hub/src/topcoat_hub/chat_review.py", "hub/tests/test_chat_review.py"]
    assert fp.spot_paths(paths, "failed", RUN_A) == ["hub/tests/test_chat_review.py"]
    assert fp.spot_paths(paths, "failed", "no names here") == paths
    edits = [{"path": paths[0], "old": "a", "new": "b"}, {"path": paths[1], "old": "c", "new": "d"}]
    only_test = fp.at_spot(edits, [], [paths[1]])
    assert only_test == {("edit", paths[1], "c", "d")}
