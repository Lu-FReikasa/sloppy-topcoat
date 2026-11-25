"""The choice of a model and node by the profile of a request (P2b, first step: pure rules)."""

from topcoat_hub.choice import Option, Profile, choose

THRESHOLD = 0.6


def opt(name: str, *, loaded: bool, quality: float | None, seconds: float | None = 10.0) -> Option:
    return Option(name=name, model=f"m-{name}", loaded=loaded, quality=quality, seconds=seconds)


def test_a_loaded_model_over_the_threshold_beats_a_better_one_that_must_load():
    options = [opt("a", loaded=True, quality=0.7), opt("b", loaded=False, quality=0.95, seconds=90)]
    choice = choose(options, Profile(urgency="soon", quality="best"), THRESHOLD)
    assert choice.action == "use_loaded"
    assert choice.option is options[0]


def test_good_enough_with_nothing_loaded_loads_the_best_rated_one():
    options = [opt("a", loaded=False, quality=0.65), opt("b", loaded=False, quality=0.8)]
    choice = choose(options, Profile(urgency="soon", quality="good_enough"), THRESHOLD)
    assert choice.action == "load"
    assert choice.option is options[1]


def test_the_best_quality_loads_when_the_load_pays_and_the_urgency_allows_it():
    loaded = opt("a", loaded=True, quality=0.7, seconds=5)
    better = opt("b", loaded=False, quality=0.95, seconds=30)
    choice = choose([loaded, better], Profile(urgency="any", quality="best"), THRESHOLD)
    assert choice.action == "load"
    assert choice.option is better


def test_the_best_quality_does_not_load_when_the_load_is_too_expensive():
    loaded = opt("a", loaded=True, quality=0.7, seconds=5)
    far = opt("b", loaded=False, quality=0.95, seconds=500)
    choice = choose([loaded, far], Profile(urgency="any", quality="best"), THRESHOLD)
    assert choice.action == "use_loaded"
    assert choice.option is loaded


def test_nothing_over_the_threshold_waits_when_the_request_may_wait():
    options = [opt("a", loaded=True, quality=0.3), opt("b", loaded=False, quality=0.5)]
    choice = choose(
        options, Profile(urgency="any", quality="good_enough", may_wait=True), THRESHOLD
    )
    assert choice.action == "wait"
    assert choice.option is None


def test_nothing_over_the_threshold_takes_the_best_that_exists_when_it_may_not_wait():
    low = opt("a", loaded=True, quality=0.3)
    lower = opt("b", loaded=True, quality=0.2)
    choice = choose(
        [low, lower], Profile(urgency="now", quality="good_enough", may_wait=False), THRESHOLD
    )
    assert choice.action == "fallback"
    assert choice.option is low
    assert "threshold" in choice.reason


def test_an_unrated_option_is_never_chosen_over_a_rated_one_above_the_threshold():
    unrated = opt("a", loaded=True, quality=None)
    rated = opt("b", loaded=False, quality=0.8)
    choice = choose([unrated, rated], Profile(urgency="soon", quality="good_enough"), THRESHOLD)
    assert choice.option is rated


def test_no_option_at_all_says_so():
    choice = choose([], Profile(), THRESHOLD)
    assert choice.action == "none"
    assert choice.option is None


def test_first_choice_names_the_node_the_profile_prefers():
    from topcoat_hub.choice import first_choice, profile_for

    options = [
        opt("a", loaded=False, quality=0.9, seconds=90),
        opt("b", loaded=True, quality=0.7, seconds=5),
    ]
    assert first_choice(options, profile_for("chat")) == "b"


def test_first_choice_is_none_when_nothing_is_rated():
    from topcoat_hub.choice import first_choice, profile_for

    assert first_choice([opt("a", loaded=True, quality=None)], profile_for("review")) is None


def test_move_first_keeps_the_others_as_fallbacks_in_their_order():
    from topcoat_hub.choice import move_first

    assert move_first(["a", "b", "c"], "c") == ["c", "a", "b"]
    assert move_first(["a", "b"], None) == ["a", "b"]
    assert move_first(["a", "b"], "zz") == ["a", "b"]


def test_order_pairs_prefers_the_best_rated_loaded_model_across_nodes():
    from topcoat_hub.choice import order_pairs, profile_for

    pairs = [("node-a", "small"), ("node-b", "big"), ("node-b", "small")]
    loaded = {("node-a", "small"): True, ("node-b", "big"): True}
    quality = {"small": 0.7, "big": 0.9}
    out = order_pairs(pairs, loaded, quality, profile_for("chat"))
    assert out[0] == ("node-b", "big")
    assert out[1:] == [("node-a", "small"), ("node-b", "small")]


def test_order_pairs_keeps_the_old_order_when_nothing_is_rated():
    from topcoat_hub.choice import order_pairs, profile_for

    pairs = [("node-a", "x"), ("node-b", "y")]
    assert order_pairs(pairs, {}, {}, profile_for("chat")) == pairs
