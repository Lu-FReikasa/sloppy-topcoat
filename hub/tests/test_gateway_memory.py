"""The memory rule of loading a model (gateway.py): a model is loaded only when it and its buffer
fit into the node's free memory; idle models make room, least recently used first."""

from topcoat_hub.gateway import free_memory, memory_budgets, plan_load, plan_room


def test_free_memory_is_the_sum_of_ram_and_vram_budgets() -> None:
    # the summed rule: a model may be split between the GPU and RAM, so both count
    summary = {
        "ram_total_mb": 32000,
        "ram_used_mb": 20000,
        "gpus": [{"vram_total_mb": 8000, "vram_used_mb": 7500}],
    }
    assert free_memory(summary) == 12500
    assert memory_budgets(summary) == (12000.0, 500.0)


def test_vram_is_summed_over_all_gpus() -> None:
    summary = {
        "ram_total_mb": 32000,
        "ram_used_mb": 30000,
        "gpus": [
            {"vram_total_mb": 8000, "vram_used_mb": 7000},
            {"vram_total_mb": 8000, "vram_used_mb": 6000},
        ],
    }
    assert memory_budgets(summary) == (2000.0, 3000.0)


def test_free_memory_without_numbers_is_unknown() -> None:
    assert free_memory({}) is None
    assert free_memory({"ram_total_mb": 32000}) is None  # used memory not reported


def test_a_model_that_fits_with_its_buffer_loads_without_unloading() -> None:
    assert plan_room(free_mb=10000, need_mb=6000, idle=[("a", 5000)]) == []


def test_idle_models_make_room_in_the_order_given() -> None:
    # 3000 free, need 9000: the first idle model (oldest) gives 5000, the second 4000
    plan = plan_room(free_mb=3000, need_mb=9000, idle=[("old", 5000), ("newer", 4000)])
    assert plan == ["old", "newer"]


def test_only_as_many_idle_models_go_as_are_needed() -> None:
    plan = plan_room(free_mb=3000, need_mb=7000, idle=[("old", 5000), ("newer", 4000)])
    assert plan == ["old"]


def test_when_idle_models_do_not_make_room_the_load_waits() -> None:
    assert plan_room(free_mb=1000, need_mb=20000, idle=[("a", 5000)]) is None
    assert plan_room(free_mb=1000, need_mb=20000, idle=[]) is None


def load(ram, total, need, idle=(), percent=20.0, floor=2048.0):
    return plan_load(ram, total, need, percent, floor, list(idle))


def test_the_buffer_is_a_share_of_the_model_size() -> None:
    # 5000 MB needs 6000 with 20 %: 5500 free is too little, 6000 is enough
    assert load(ram=5500, total=5500, need=5000) is None
    assert load(ram=6000, total=6000, need=5000) == []


def test_a_second_small_model_loads_next_to_the_first_when_both_fit() -> None:
    # no fixed count: the big model is not idle, the small one still fits with its buffer
    assert load(ram=10000, total=10000, need=2000) == []


def test_a_node_below_the_ram_floor_loads_nothing_when_nothing_is_idle() -> None:
    assert load(ram=1500, total=1500, need=500) is None


def test_an_idle_model_is_unloaded_to_reach_the_ram_floor() -> None:
    # 1500 MB free RAM, the floor is 2048: the idle 3000 MB model goes, which also covers the need
    assert load(ram=1500, total=1500, need=1000, idle=[("old", 3000)]) == ["old"]


def test_a_node_without_a_reported_ram_has_no_floor() -> None:
    assert load(ram=None, total=3000, need=2000) == []


def test_an_unknown_model_size_still_keeps_the_ram_floor() -> None:
    assert load(ram=1000, total=1000, need=None) is None
    assert load(ram=5000, total=5000, need=None) == []
