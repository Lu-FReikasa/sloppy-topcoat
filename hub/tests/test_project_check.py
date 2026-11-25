"""The second look at a project task's result (project_check.py): a row is read against the
excerpts it cites; a verdict is needed for every row; a result never passes without one."""

from topcoat_hub.project_check import parse_verdicts, row_lines


def test_each_row_comes_with_the_excerpts_it_cites() -> None:
    table = [
        {"Option": "-l", "Wirkung": "lang", "Beleg": "[E4] E2"},
        {"Option": "-a", "Beleg": "E9"},
    ]
    text = row_lines(table, {"E4": "ls -l shows permissions", "E2": "-a, --all"})
    assert "1. Option: -l; Wirkung: lang" in text
    assert "E4:\nls -l shows permissions" in text
    assert "E2:\n-a, --all" in text
    assert "(no kept read is cited)" in text  # E9 was never kept: the row says so


def test_an_excerpt_cited_by_many_rows_stands_once() -> None:
    sheet = "a long fact sheet " * 50
    table = [{"Aussage": f"a{i}", "Beleg": f"E1 (Zeile {i}), E2"} for i in range(16)]
    text = row_lines(table, {"E1": sheet, "E2": "short"})
    assert text.count(sheet) == 1 and text.count("E2:\nshort") == 1
    assert text.index("Sources:") < text.index("Rows:")
    assert "16. Aussage: a15" in text and "Beleg: E1 (Zeile 15), E2" in text


def test_a_verdict_is_needed_for_every_row() -> None:
    ok = '{"rows":[{"n":1,"supported":true},{"n":2,"supported":false,"why":"x"}]}'
    assert parse_verdicts(ok, 2) == {1: True, 2: False}
    assert parse_verdicts(ok, 3) is None  # a row without a verdict
    assert parse_verdicts('{"rows":[{"n":1,"supported":"maybe"}]}', 1) is None
    assert parse_verdicts("I think the rows are fine", 1) is None


def test_the_second_look_may_use_the_authors_model_on_another_node() -> None:
    from topcoat_hub.chat_review import pick_reviewer

    nodes = [
        {"node": "node-nvidia", "loaded": False},
        {"node": "node-amd", "loaded": False},
    ]
    models = [{"id": "gemma", "topcoat": {"nodes": nodes}}]
    chain = [("node-nvidia", "gemma"), ("node-amd", "gemma")]
    # the chat keeps its rule: the author's model does not check; the second look may, elsewhere
    assert pick_reviewer(models, "gemma", "", chain, author_node="node-nvidia") is None
    assert (
        pick_reviewer(
            models, "gemma", "", chain, author_node="node-nvidia", same_model_elsewhere=True
        )
        == "gemma@node-amd"
    )


def test_the_verdicts_are_read_in_the_shapes_the_reviewers_write() -> None:
    fenced = (
        '```json\n{"rows": [{"n": "1", "supported": "yes"}, {"row": 2, "verdict": "nein"}]}\n```'
    )
    assert parse_verdicts(fenced, 2) == {1: True, 2: False}
    other_name = '{"verdicts": [{"n": 1, "supported": true}, {"n": 2, "supported": true}]}'
    assert parse_verdicts(other_name, 2) == {1: True, 2: True}


def test_a_broken_answer_still_gives_the_verdicts_it_holds() -> None:
    from topcoat_hub.project_check import loose_verdicts

    two_in_one = (
        '{"rows":[{"n":1,"supported":true,"why":"a","n":2,"supported":false,"why":"b"},'
        '{"n":"3","supp'
    )
    assert loose_verdicts(two_in_one) == {1: True, 2: False}
    assert loose_verdicts("no verdicts") == {}


def test_the_verdicts_of_a_broken_answer_and_of_the_second_ask_decide_the_look() -> None:
    import asyncio
    from types import SimpleNamespace

    from topcoat_hub.project_check import SecondLook

    answers = [
        # three rows, the answer breaks after the second: row 3 is asked again, alone
        '{"rows":[{"n":1,"supported":true,"why":"a"},{"n":2,"supported":false,"why":"E1 lacks it"'
        ',{"n":3,"supp',
        '{"rows":[{"n":1,"supported":false,"why":"not in E2"}]}',
    ]
    asked: list[str] = []

    async def call(model, path, payload, label=""):  # type: ignore[no-untyped-def]
        asked.append(payload["messages"][1]["content"])
        return {"choices": [{"message": {"content": answers[len(asked) - 1]}}]}

    look = SecondLook(SimpleNamespace(call=call), SimpleNamespace())  # type: ignore[arg-type]
    table = [{"Text": t, "Beleg": "E1"} for t in ("x", "y", "z")]
    got = asyncio.run(look.check("q", table, {"E1": "x"}, "author", "rev@node"))
    assert len(asked) == 2 and "1. Text: z" in asked[1]  # only the unjudged row, renumbered
    assert not got.ok and got.objections == {2: "E1 lacks it", 3: "not in E2"}
