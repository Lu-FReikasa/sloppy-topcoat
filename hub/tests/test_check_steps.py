from topcoat_hub import check_order, check_steps


def test_typical_is_the_75th_percentile_within_the_limit() -> None:
    assert check_steps.typical_of([1, 2, 3]) is None  # too few to say anything
    assert check_steps.typical_of([2, 4, 4, 5, 7, 9, 20, 3]) == 7
    assert check_steps.typical_of([0, 0, 0, 0, 0]) == 1
    assert check_steps.typical_of([30, 30, 30, 30, 30], cap=16) == 16


def test_the_card_carries_the_usual_calls() -> None:
    order = check_order.Order(target="hub", tools=("notes",), goal="g", result="text", window="")
    card = check_order.card(order, 16, 7)
    assert card["max_calls"] == 16
    assert card["typical_calls"] == 7
