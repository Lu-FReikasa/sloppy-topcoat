def test_a_field_of_a_json_text_is_read_from_the_json() -> None:
    """Live 2026-11-17: the items of a model's JSON list are kept as text; {{place.lines}} of an
    item read nothing and the read of an issue program failed."""
    from topcoat_hub.templating import render, resolve

    place = '{"path": "hub/src/topcoat_hub/project_tools.py", "lines": "180-260"}'
    assert render("{{place.path}} {{place.lines}}", {"place": place}) == (
        "hub/src/topcoat_hub/project_tools.py 180-260"
    )
    assert resolve("x.a", {"x": "not json"}) is None
    assert resolve("x.a", {"x": "{broken"}) is None
    assert resolve("x[1]", {"x": '["a", "b"]'}) == "b"
