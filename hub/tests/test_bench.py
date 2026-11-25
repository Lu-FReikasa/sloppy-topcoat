"""The bench (PRG-00 R6): scoring by rules, shipped suites, the `rated` pick, runs on fake nodes."""

import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import online_node
from topcoat_hub.bench import scoring, suites
from topcoat_hub.blocks import pick

# --- scoring ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "checks", "passed"),
    [
        ("Canberra is the capital.", {"must": ["canberra"]}, True),
        ("Sydney.", {"must": ["canberra"]}, False),
        ("It is 80 km/h", {"must": [["80 km/h", "80 kilometers per hour"]]}, True),
        ("Not sure", {"must_not": ["sure"]}, False),
        ("a b c", {"max_words": 2}, False),
        ("a", {"min_words": 2}, False),
        ("The answer is 4 DONE", {"regex": r"4[\s\S]*DONE\W*$"}, True),
        ('Here: {"a": 1} ok', {"json": {"keys": ["a"]}}, True),
        ('{"a": 1}', {"json": {"keys": ["a", "b"]}}, False),
        ("{not json", {"json": {}}, False),
        ("   ", {}, False),
        ("First 3+7.\n\nSo the answer is 10", {"final": r"\b10\b"}, True),
        ("10 is my first guess.\nNo, 11.", {"final": r"\b10\b"}, False),
    ],
)
def test_scoring_follows_the_rules(answer: str, checks: dict[str, Any], passed: bool) -> None:
    assert scoring.score_case(answer, checks).passed is passed


def test_a_failed_case_says_which_check_failed() -> None:
    got = scoring.score_case("Sydney", {"must": ["canberra"], "max_words": 0})
    assert got.failed == ["must:canberra", "max_words:1>0"]


# --- the shipped suites -----------------------------------------------------------------------

GOLDEN = {
    "capital": "Canberra is the capital of Australia.",
    "speed": "The average speed is 80 km/h.",
    "dns": "DNS translates a domain name into an IP address so that computers can find each "
    "other on a network. It works like a phone book for the internet.",
    "weights": "About 3.5 GB.",
    "colours": "red, green, blue",
    "invented": "I don't know of any record of that town, it may not exist.",
    "translate": "Der Server startet jede Nacht neu.",
    "code": "def is_even(n):\n    return n % 2 == 0",
    "person": '{"name": "Maria Lopez", "age": 34, "city": "Madrid"}',
    "planets": '{"items": ["Mercury", "Mars", "Venus"]}',
    "prime": '{"answer": true, "reason": "17 has no divisors but 1 and itself"}',
    "nested": '{"server": {"host": "db.example.lan", "port": 5432}}',
    "nothing-else": '{"ok": true}',
    "quote": '{"quote": "He said \\"stop\\" twice."}',
    "water-boils": "OK",
    "len-bytes": "ERROR: len() counts items, not bytes.",
    "tcp": "ERROR: TCP is connection-oriented and reliable.",
    "kilobyte": "OK",
    "chmod": "ERROR: chmod changes permissions, chown the owner.",
    "http-404": "OK",
    "one-word": "Blue",
    "no-z": "Tea is a warm drink made from leaves. People enjoy it every day.",
    "numbers": "1, 2, 3, 4, 5",
    "german-only": "Die Hauptstadt von Frankreich ist Paris.",
    "forbidden-word": "Apple, pear and cherry.",
    "end-marker": "2+2 is 4. DONE",
    "pens": "9.60 euros",
    "heights": "Bob",
    "pipes": "Together they need 2 hours.",
    "apples": "5",
    "product": "391",
    "letters": "3",
    "weekday": "Friday",
    "bat-ball": "5",
    "sort": "3, 7, 19, 42, 100",
    "meeting": "It ends at 16:05.",
    "ctx-code": "7391-ALPHA",
    "ctx-ports": "boreas",
    "ctx-changed": "maple41",
    "weather": '{"tool": "get_weather", "args": {"city": "Berlin", "unit": "fahrenheit"}}',
    "email": '{"tool": "send_email", "args": {"to": "anna@example.lan", "subject": "Lunch", '
    '"body": "Noon works for me."}}',
    "search": '{"tool": "search_files", "args": {"query": "invoices", "max_results": 5}}',
    "no-tool": '{"tool": null, "args": {}}',
    "celsius": '{"tool": "get_weather", "args": {"city": "Paris", "unit": "celsius"}}',
    "de-proxy": "Ein Reverse Proxy nimmt Anfragen von Clients entgegen und leitet sie an die "
    "passenden Server weiter. So lassen sich mehrere Dienste hinter einer Adresse bündeln.",
    "de-sydney": "FEHLER: Die Hauptstadt ist Canberra.",
    "de-byte": "OK",
    "de-translate": "The service starts automatically after the restart.",
    "de-commands": "cat, less, head",
    "de-leap": "366",
    "udp-order": "ERROR: UDP does not guarantee order.",
    "list-immutable": "ERROR: lists are mutable.",
    "sql-where": "OK",
    "git-rebase": "OK",
    "docker-kernel": "ERROR: containers share the kernel of the host.",
    "prime-def": "OK",
    "http-get": "OK",
    "raid0": "ERROR: RAID 0 has no redundancy.",
    "json-total": '{"total": 3.0, "items": ["apple", "pear"]}',
    "json-leap": '{"leap": false}',
    "json-null": '{"name": "Carla", "age": null}',
    "json-people": '{"people": [{"name": "Ann", "age": 31}, {"name": "Ben", "age": 27}]}',
    "seven-words": "Rain falls softly on the quiet roof.",
    "numbered": "1. red\n2. green\n3. blue",
    "ctx-multihop": "Mara Venn",
    "ctx-count": "4",
    "ctx-latest": "1.9",
    "ctx-first": "lantern-47",
    "ctx-last": "58",
    "tool-capital": '{"tool": "get_weather", "args": {"city": "Tokyo", "unit": "celsius"}}',
    "quotes": '{"tool": "send_email", "args": {"to": "bea@example.lan", "subject": "Status", '
    '"body": "He said \\"ok\\"."}}',
    "top-five": '{"tool": "search_files", "args": {"query": "router", "max_results": 5}}',
    "left-join": "ERROR: a left join also keeps rows without a match.",
    "http-301": "ERROR: 301 is a permanent redirect.",
    "volume": "ERROR: volumes outlive their containers.",
    "py-div": "ERROR: it evaluates to 3.5.",
    "dns-a": "OK",
    "tls-rc4": "ERROR: TLS 1.3 removed RC4.",
    "ls-a": "OK",
    "mutex": "ERROR: a mutex allows one holder at a time.",
}


def all_cases() -> list[tuple[str, dict[str, Any]]]:
    return [(key, case) for key, s in suites.SUITES.items() for case in s["cases"]]


def test_every_shipped_case_can_be_passed_and_is_not_passed_by_nothing() -> None:
    ids = [c["id"] for _k, c in all_cases()]
    assert len(ids) == len(set(ids)), "case ids are unique"
    assert set(ids) == set(GOLDEN), "every case has a golden answer here (and no stale ones)"
    for _key, case in all_cases():
        assert scoring.score_case(GOLDEN[case["id"]], case["checks"]).passed, case["id"]
        assert not scoring.score_case("", case["checks"]).passed, case["id"]


def test_a_vague_non_answer_does_not_pass_the_invented_town_case_by_luck() -> None:
    case = next(c for _k, c in all_cases() if c["id"] == "invented")
    assert not scoring.score_case("The population was 1,204 people.", case["checks"]).passed


# --- the pick `rated` -------------------------------------------------------------------------

POOL = [
    pick.Candidate("big", "node-a", size_mb=20000, tps=30, ratings={"answer": 0.5, "json": 1.0}),
    pick.Candidate("mid", "node-a", size_mb=8000, tps=60, ratings={"answer": 0.9}),
    pick.Candidate("small", "node-b", size_mb=2000, tps=120),
]


def test_rated_takes_the_best_score_of_the_suite_not_the_largest_model() -> None:
    got = pick.evaluate({"rank": "rated", "suite": "answer"}, POOL)
    assert got is not None and got.model == "mid" and "best rated for answer" in got.reason
    got = pick.evaluate({"rank": "rated", "suite": "json"}, POOL)
    assert got is not None and got.model == "big"


def test_rated_without_any_rating_falls_back_to_the_best_model_and_says_so() -> None:
    got = pick.evaluate({"rank": "rated", "suite": "review"}, POOL)
    assert got is not None and "no rating for review" in got.note


def test_a_pick_names_only_a_suite_that_exists() -> None:
    with pytest.raises(pick.PickError):
        pick.check_spec({"rank": "rated", "suite": "nonsense"}, "x")


# --- runs on fake nodes -----------------------------------------------------------------------

QUESTIONS = {c["question"]: GOLDEN[c["id"]] for _k, c in all_cases()}


def golden_answer(req: dict[str, Any]) -> str | None:
    return QUESTIONS.get(req["messages"][-1]["content"])


def wait_for_run(client: TestClient, run_id: str, seconds: float = 30.0) -> dict[str, Any]:
    end = time.time() + seconds
    while time.time() < end:
        view = client.get(f"/api/bench/runs/{run_id}", headers=AUTH).json()
        if view["status"] != "running":
            return view  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError("the run did not finish")


def start(client: TestClient, model: str = "small", node: str = "node-a", keys=("answer",)):
    return client.post(
        "/api/bench/runs", headers=AUTH, json={"model": model, "node": node, "suites": list(keys)}
    )


def test_a_run_rates_the_model_and_the_rating_is_kept(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small", "big"])
    node.stream_text_fn = golden_answer
    res = start(client, keys=("answer", "json"))
    assert res.status_code == 202, res.text
    done = wait_for_run(client, res.json()["run_id"])
    assert done["status"] == "done" and done["error"] is None
    assert [r["suite"] for r in done["results"]] == ["answer", "json"]
    assert all(r["score"] == 1.0 and not r["failed"] for r in done["results"])
    rows = client.get("/api/bench/ratings", headers=AUTH).json()
    assert {(r["model"], r["node"], r["suite"], r["score"], r["current"]) for r in rows} == {
        ("small", "node-a", "answer", 1.0, True),
        ("small", "node-a", "json", 1.0, True),
    }


def test_a_poor_model_gets_a_low_score_and_the_failed_cases_are_named(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    node.stream_text_fn = lambda req: "I am not sure."
    done = wait_for_run(client, start(client, keys=("review",)).json()["run_id"])
    result = done["results"][0]
    assert result["score"] < 0.5 and result["failed"]
    assert {f["case"] for f in result["failed"]} >= {"tcp"}


def test_a_model_outside_the_pool_or_an_unknown_suite_is_refused(
    client: TestClient, connect
) -> None:
    online_node(client, connect, "node-a", loaded=["small", "big"])
    client.put(
        "/api/scheduling/pools", headers=AUTH, json={"pools": {"node-a": {"available": ["big"]}}}
    )
    assert start(client, model="small").status_code == 409
    assert start(client, model="big", keys=("nonsense",)).status_code == 422
    assert start(client, model="big", node="node-zzz").status_code == 409


def test_a_node_serves_one_run_at_a_time_and_a_run_can_be_stopped(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    gate = threading.Event()

    def slow(req: dict[str, Any]) -> str | None:
        gate.wait(5)
        return golden_answer(req)

    node.stream_text_fn = slow
    first = start(client).json()
    assert start(client).status_code == 409  # the node is busy with the first
    stopped = client.delete(f"/api/bench/runs/{first['run_id']}", headers=AUTH)
    assert stopped.json() == {"stopped": True}
    gate.set()
    assert wait_for_run(client, first["run_id"])["status"] == "stopped"
    assert (
        client.get("/api/bench/ratings", headers=AUTH).json() == []
    )  # a stopped suite is not rated


def test_the_bench_is_for_admins_only(client: TestClient) -> None:
    for method, url in (("get", "/api/bench/suites"), ("get", "/api/bench/ratings")):
        assert getattr(client, method)(url).status_code == 401
    listing = client.get("/api/bench/suites", headers=AUTH).json()
    assert {s["key"] for s in listing} == set(suites.SUITES)


def test_a_rating_of_an_older_suite_version_is_shown_but_does_not_rank(
    client: TestClient, connect
) -> None:
    import uuid

    from topcoat_hub.models import ModelRating

    online_node(client, connect, "node-a", loaded=["small"])

    async def old_rating() -> None:
        async with client.app.state.sessionmaker() as db:
            db.add(
                ModelRating(
                    run_id=uuid.uuid4(),
                    model="small",
                    node="node-a",
                    suite="answer",
                    function_kind="answer",
                    suite_version=suites.SUITES["answer"]["version"] - 1,
                    score=0.9,
                    passed=9,
                    total=10,
                )
            )
            await db.commit()

    client.portal.call(old_rating)
    (row,) = client.get("/api/bench/ratings", headers=AUTH).json()
    assert row["score"] == 0.9 and row["current"] is False


def test_a_block_program_may_pick_by_rating() -> None:
    from topcoat_hub.blocks import program

    spec = {
        "name": "p",
        "inputs": {"question": "text"},
        "stack": [
            {
                "b": "ask",
                "prompt": "{{question}}",
                "pick": {"rank": "rated", "suite": "json"},
                "out": "a",
            }
        ],
    }
    assert program.validate(spec)["stack"][0]["pick"]["suite"] == "json"
    assert "rated" in program.catalogue()["ranks"] and "json" in program.catalogue()["suites"]


def test_the_pause_of_background_work_does_not_stop_a_bench_someone_started(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    node.stream_text_fn = golden_answer
    assert (
        client.post("/api/scheduling/pause", headers=AUTH, json={"minutes": 10}).status_code == 200
    )
    done = wait_for_run(client, start(client).json()["run_id"])
    assert done["status"] == "done" and done["results"][0]["score"] == 1.0


# --- the suggestion ---------------------------------------------------------------------------


def rated(model: str, node: str, score: float, current: bool = True) -> list[dict[str, Any]]:
    from topcoat_hub.bench import suggest

    return [
        {"model": model, "node": node, "suite": s, "score": score, "current": current}
        for s in suggest.GENERAL_SUITES
    ]


def test_a_clearly_better_model_is_suggested_with_the_numbers() -> None:
    from topcoat_hub.bench import suggest

    ratings = rated("big", "node-a", 0.9) + rated("small", "node-a", 0.6)
    got = suggest.suggest_default(ratings, "small", {"node-a": {"big", "small"}})
    assert got is not None and (got["model"], got["node"]) == ("big", "node-a")
    assert (got["score"], got["default_score"]) == (0.9, 0.6)


def test_no_suggestion_for_a_small_difference_the_best_default_or_half_measured_models() -> None:
    from topcoat_hub.bench import suggest

    pool = {"node-a": {"big", "small"}}
    close = rated("big", "node-a", 0.62) + rated("small", "node-a", 0.6)
    assert suggest.suggest_default(close, "small", pool) is None  # inside the margin
    best = rated("big", "node-a", 0.9) + rated("small", "node-a", 0.6)
    assert suggest.suggest_default(best, "big", pool) is None  # the default is the best already
    partial = [r for r in rated("big", "node-a", 1.0) if r["suite"] != "reason"]
    assert suggest.suggest_default(partial + rated("small", "node-a", 0.5), "small", pool) is None
    old = rated("big", "node-a", 1.0, current=False) + rated("small", "node-a", 0.5)
    assert suggest.suggest_default(old, "small", pool) is None  # an old suite version cannot rank


def test_a_model_that_fewer_nodes_serve_than_the_default_is_not_suggested() -> None:
    from topcoat_hub.bench import suggest

    ratings = rated("big", "node-a", 0.95) + rated("small", "node-a", 0.6)
    ratings += rated("small", "node-b", 0.6)
    pool = {"node-a": {"big", "small"}, "node-b": {"small"}}  # big runs on one machine only
    assert suggest.suggest_default(ratings, "small", pool) is None
    pool_both = {"node-a": {"big", "small"}, "node-b": {"big", "small"}}
    assert suggest.suggest_default(ratings + rated("big", "node-b", 0.95), "small", pool_both)


def test_only_models_of_the_pool_are_suggested_and_an_unrated_default_gives_way() -> None:
    from topcoat_hub.bench import suggest

    ratings = rated("big", "node-a", 0.9) + rated("out", "node-a", 1.0)
    got = suggest.suggest_default(ratings, "never-rated", {"node-a": {"big", "small"}})
    assert got is not None and got["model"] == "big" and got["default_score"] is None


def test_the_suggestion_is_an_endpoint_and_changes_nothing(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small", "big"])
    assert client.get("/api/bench/suggestion", headers=AUTH).json() == {"suggestion": None}
    assert client.get("/api/bench/suggestion").status_code == 401


# --- variants ---------------------------------------------------------------------------------


def test_a_variant_changes_one_setting_of_the_request_and_never_ranks(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    seen: list[dict[str, Any]] = []

    def answer(req: dict[str, Any]) -> str | None:
        seen.append(req)
        return golden_answer(req)

    node.stream_text_fn = answer
    run = start(client, keys=("answer",))
    assert wait_for_run(client, run.json()["run_id"])["status"] == "done"
    res = client.post(
        "/api/bench/runs",
        headers=AUTH,
        json={"model": "small", "node": "node-a", "suites": ["answer"], "variant": "nothink"},
    )
    assert res.status_code == 202 and res.json()["variant"] == "nothink"
    assert wait_for_run(client, res.json()["run_id"])["status"] == "done"
    # the preset reached the node; the plain run did not carry it
    assert any(r.get("chat_template_kwargs") == {"enable_thinking": False} for r in seen)
    assert not any("chat_template_kwargs" in r for r in seen[:8])
    rows = client.get("/api/bench/ratings", headers=AUTH).json()
    assert {(r["variant"], r["suite"]) for r in rows} == {("", "answer"), ("nothink", "answer")}


def test_a_tried_setting_does_not_enter_the_suggestion() -> None:
    from topcoat_hub.bench import suggest

    plain = rated("small", "node-a", 0.5)
    tried = [{**r, "variant": "nothink"} for r in rated("big", "node-a", 1.0)]
    assert suggest.suggest_default(plain + tried, "small", {"node-a": {"big", "small"}}) is None


def test_the_variants_are_presets_and_an_unknown_one_is_refused(
    client: TestClient, connect
) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    names = {v["key"] for v in client.get("/api/bench/variants", headers=AUTH).json()}
    assert {"", "nothink", "think-low", "cold", "warm"} <= names
    bad = client.post(
        "/api/bench/runs",
        headers=AUTH,
        json={"model": "small", "node": "node-a", "suites": ["answer"], "variant": "rm -rf"},
    )
    assert bad.status_code == 422
