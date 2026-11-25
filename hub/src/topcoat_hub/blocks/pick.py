"""A pick chooses a model (and a node) from the pool by a rule (PRG-00, the pick evaluator).

Pure: the caller hands in the candidates (what the pool offers, one per model and node), the engine
never reaches for the gateway here. A pick is a small dict:

``fixed``      ``"model"`` or ``"model@node"``: no choice, exactly that
``same_as``    set from a variable: the model that wrote it (``other_than`` is the opposite)
``kind``       what the model must be for (``model_facts.KINDS``), default ``chat``
``rank``       ``best`` (default: largest still fast enough), ``fastest``, ``largest``, ``loaded``,
               ``rated`` (the best score of the bench suite named by ``suite``, default ``answer``;
               with no rating yet it falls back to ``best`` and says so)
``suite``      the bench suite ``rated`` looks at (``bench.suites.SUITES``)
``node``       only candidates on this node
``min_tps``    drop models measured slower than this (unmeasured ones stay)
``exclusive``  unload the other models of the node first (a small card holds one model at a time)
``other_than`` names of variables: avoid the models that wrote them (the engine makes ``avoid``)
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from topcoat_hub.bench.suites import SUITES
from topcoat_hub.model_resolver import ModelFacts, best_model

PICK_KEYS = frozenset(
    {"fixed", "same_as", "kind", "rank", "node", "min_tps", "other_than", "exclusive", "suite"}
)
RANKS = ("best", "fastest", "largest", "loaded", "rated")


@dataclass(frozen=True)
class Candidate:
    model: str
    node: str
    kinds: tuple[str, ...] = ("chat",)
    size_mb: float | None = None
    tps: float | None = None
    loaded: bool = False
    # bench suite -> score (0 to 1) of the newest version of the suite on this node
    ratings: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Picked:
    model: str
    node: str | None
    reason: str
    note: str = ""  # something the person should know (a judge that is not independent)
    exclusive: bool = False

    @property
    def requested(self) -> str:
        return f"{self.model}@{self.node}" if self.node else self.model


class PickError(ValueError):
    """The pick is not valid."""


def check_spec(spec: Any, where: str) -> dict[str, Any]:
    """A pick as written in a program, or an error naming where."""
    if spec is None:
        return {}
    if not isinstance(spec, dict) or set(spec) - PICK_KEYS:
        raise PickError(f"{where}: a pick has {', '.join(sorted(PICK_KEYS))}")
    if "rank" in spec and spec["rank"] not in RANKS:
        raise PickError(f"{where}: rank is one of {', '.join(RANKS)}")
    for key in ("fixed", "same_as", "node"):
        if key in spec and (not isinstance(spec[key], str) or not spec[key].strip()):
            raise PickError(f"{where}: {key} must be a name")
    if "other_than" in spec and (
        not isinstance(spec["other_than"], list)
        or not all(isinstance(x, str) and x for x in spec["other_than"])
    ):
        raise PickError(f"{where}: other_than is a list of variable names")
    if "exclusive" in spec and not isinstance(spec["exclusive"], bool):
        raise PickError(f"{where}: exclusive is true or false")
    if "suite" in spec and spec["suite"] not in SUITES:
        raise PickError(f"{where}: suite is one of {', '.join(SUITES)}")
    tps = spec.get("min_tps")
    if tps is not None and (isinstance(tps, bool) or not isinstance(tps, int | float) or tps < 0):
        raise PickError(f"{where}: min_tps is a number")
    return dict(spec)


def _node_of(model: str, rows: list[Candidate]) -> str | None:
    on = [c for c in rows if c.model == model]
    if not on:
        return None
    return max(on, key=lambda c: (c.loaded, c.tps or 0.0, c.node)).node


def evaluate(
    spec: Mapping[str, Any],
    candidates: list[Candidate],
    *,
    avoid: Collection[str] = (),
    same_as: str | None = None,
) -> Picked | None:
    """See :func:`_evaluate`; the pick's ``exclusive`` flag is carried to the result."""
    found = _evaluate(spec, candidates, avoid=avoid, same_as=same_as)
    if found is not None and spec.get("exclusive"):
        found = replace(found, exclusive=True)
    return found


def _evaluate(
    spec: Mapping[str, Any],
    candidates: list[Candidate],
    *,
    avoid: Collection[str] = (),
    same_as: str | None = None,
) -> Picked | None:
    """The model and node the pick stands for, or ``None`` when the pool has nothing for it.
    ``avoid`` are model names that wrote the inputs (``other_than``); with no other model the best
    remaining one is returned and the note says so (a judge from the family of an author prefers
    its own text)."""
    fixed = spec.get("fixed")
    if fixed:
        model, _, node = fixed.partition("@")
        return Picked(model, node or None, "fixed")
    if same_as:
        model = same_as.partition("@")[0]
        return Picked(model, _node_of(model, candidates), "the same model as the author")

    kind = spec.get("kind", "chat")
    rows = [c for c in candidates if kind in c.kinds]
    if spec.get("node"):
        rows = [c for c in rows if c.node == spec["node"]]
    floor = spec.get("min_tps")
    if floor:
        rows = [c for c in rows if c.tps is None or c.tps >= floor]
    if not rows:
        return None

    note = ""
    wanted = [c for c in rows if c.model not in avoid]
    if avoid and not wanted:
        wanted = rows
        note = "no other model in the pool: the result is not independent of its author"
    rank = spec.get("rank", "best")
    suite = spec.get("suite", "answer")
    if rank == "rated":
        scored = [c for c in wanted if suite in c.ratings]
        if scored:
            top = max(scored, key=lambda c: (c.ratings[suite], c.tps or 0.0, c.loaded, c.model))
            return Picked(
                top.model,
                top.node,
                f"best rated for {suite} ({top.ratings[suite]:.0%})"
                + (f" on {spec['node']}" if spec.get("node") else ""),
                note,
            )
        rank = "best"
        note = note or f"no rating for {suite} yet: the best model of the pool by size and speed"
    if rank == "fastest":
        top = max(wanted, key=lambda c: (c.tps or 0.0, c.loaded, c.model))
    elif rank == "largest":
        top = max(wanted, key=lambda c: (c.size_mb or 0.0, c.loaded, c.model))
    elif rank == "loaded":
        top = max(wanted, key=lambda c: (c.loaded, c.tps or 0.0, c.model))
    else:
        by_model = {c.model: c for c in wanted}
        facts = [ModelFacts(c.model, c.size_mb, c.tps, c.loaded) for c in by_model.values()]
        best = best_model(facts, "answer") if kind == "chat" else None
        top = by_model[best.name] if best else max(wanted, key=lambda c: (c.loaded, c.model))
        top = next(
            c for c in wanted if c.model == top.model and c.node == _node_of(top.model, wanted)
        )
    return Picked(
        top.model,
        top.node,
        f"{rank} of the pool" + (f" on {spec['node']}" if spec.get("node") else ""),
        note,
    )
