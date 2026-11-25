"""A suggestion from the ratings (PRG-00 R6): which model of the pool should be its default.

Pure. A model is judged by the mean score over the general suites, and only when every one of them
has a rating of the current suite version on that node (a model measured on part of the suites
cannot be compared). The suggestion needs a margin over the current default: a small difference is
noise of a small test (the same model scores one case apart between runs). A model that fewer nodes
serve than the default is not suggested: a default that one machine serves falls with that machine
(a gaming PC). It changes nothing by itself: the admin takes it with one click (D90)."""

from collections.abc import Collection, Mapping
from typing import Any

# the suites that say how well a model does the work of a default: answering, following
# instructions, reasoning, German, facts in a long text and calling a tool (the default model also
# runs the agents, and a model that cannot call a tool is no default: gpt-oss-20b answered 98 % in
# the others and 1 of 8 here). Reviewing and JSON are shown, but do not decide.
GENERAL_SUITES = ("answer", "instruct", "reason", "german", "context", "tools")
MARGIN = 0.05


def overall(
    ratings: list[dict[str, Any]], model: str, node: str, suites: Collection[str] = GENERAL_SUITES
) -> float | None:
    """The mean score of ``model`` on ``node`` over ``suites``, or ``None`` if one is missing."""
    scores = {
        r["suite"]: r["score"]
        for r in ratings
        if r["model"] == model
        and r["node"] == node
        and r.get("current", True)
        and not r.get("variant")  # a setting that was tried does not rank
    }
    if not suites or any(s not in scores for s in suites):
        return None
    return sum(scores[s] for s in suites) / len(suites)


def suggest_default(
    ratings: list[dict[str, Any]],
    default_model: str,
    pool: Mapping[str, Collection[str]],
    suites: Collection[str] = GENERAL_SUITES,
    margin: float = MARGIN,
) -> dict[str, Any] | None:
    """The model of the pool (``node -> models``) with the best overall score, if it beats the
    current default by ``margin`` (or the default has no rating at all); else ``None``."""
    best: tuple[float, str, str] | None = None
    default_score: float | None = None
    served = {
        m: sum(1 for models in pool.values() if m in models) for ms in pool.values() for m in ms
    }
    needed = max(1, served.get(default_model, 1))
    for node, models in pool.items():
        for model in models:
            score = overall(ratings, model, node, suites)
            if score is None or served[model] < needed:
                continue
            if model == default_model and (default_score is None or score > default_score):
                default_score = score
            if best is None or (score, model, node) > best:
                best = (score, model, node)
    if best is None:
        return None
    score, model, node = best
    if model == default_model:
        return None
    if default_score is not None and score < default_score + margin:
        return None
    return {
        "model": model,
        "node": node,
        "score": round(score, 3),
        "default_model": default_model,
        "default_score": round(default_score, 3) if default_score is not None else None,
        "suites": list(suites),
    }
