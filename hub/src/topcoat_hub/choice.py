"""Choosing a model-and-node pair for a request by its profile (plan "Aufgaben statt Anwendungen",
phase P2b, first step: the rules as a pure function; nothing calls it yet, so nothing changes).

The profile has two wishes and what follows from them (plan 6.2, 6.3):

* ``urgency``: ``now`` (someone waits), ``soon`` (minutes are fine), ``any`` (may queue);
* ``quality``: ``good_enough`` (any option over the threshold) or ``best`` (the best one);
* ``may_wait``: whether the request may stand in the queue when nothing over the threshold is
  ready now.

Rules, in order:

1. A loaded option over the threshold beats any unloaded one, when the urgency is ``now`` or
   ``soon``; among them the best quality wins, then the faster reply.
2. Loading is a reason, not a default: an unloaded option is taken only when no loaded option is
   over the threshold, or when the profile wants the best quality, the urgency allows the load,
   and the load pays off (its time to a reply is not longer than the loaded best).
3. Nothing over the threshold: ``may_wait`` waits; otherwise the best option that exists is taken
   and the reason says so. Nothing is taken silently below the threshold.

An option without a quality rating is never over the threshold; it is used only through rule 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Urgency = Literal["now", "soon", "any"]
Quality = Literal["good_enough", "best"]


@dataclass(frozen=True)
class Option:
    """A model on a node that could answer now or after a load."""

    name: str  # the node
    model: str
    loaded: bool
    quality: float | None  # rating of the model for this kind of work; None when unrated
    seconds: float | None  # estimated time to a typical reply (load included), None if unknown


@dataclass(frozen=True)
class Profile:
    urgency: Urgency = "soon"
    quality: Quality = "good_enough"
    may_wait: bool = True


@dataclass(frozen=True)
class Choice:
    option: Option | None
    action: Literal["use_loaded", "load", "wait", "fallback", "none"]
    reason: str


def choose(options: list[Option], profile: Profile, threshold: float) -> Choice:
    if not options:
        return Choice(None, "none", "no option can answer this request")
    rated_ok = [o for o in options if o.quality is not None and o.quality >= threshold]
    loaded_ok = [o for o in rated_ok if o.loaded]
    unloaded_ok = [o for o in rated_ok if not o.loaded]

    def best(items: list[Option]) -> Option:
        return max(items, key=lambda o: (o.quality or 0.0, -(o.seconds or float("inf"))))

    fast = profile.urgency in ("now", "soon")
    if loaded_ok and (fast or profile.quality == "good_enough"):
        return Choice(best(loaded_ok), "use_loaded", "loaded and over the threshold")
    if unloaded_ok and profile.urgency != "now" and profile.quality == "best":
        candidate = best(unloaded_ok)
        loaded_best = best(loaded_ok) if loaded_ok else None
        pays = loaded_best is None or (
            candidate.quality is not None
            and (loaded_best.quality or 0.0) < candidate.quality
            and (candidate.seconds or float("inf"))
            <= (loaded_best.seconds or float("inf")) + _LOAD_BUDGET
        )
        if pays:
            return Choice(candidate, "load", "the best quality is worth the load")
    if (
        unloaded_ok
        and not loaded_ok
        and (profile.urgency == "any" or profile.quality == "good_enough")
    ):
        return Choice(best(unloaded_ok), "load", "nothing loaded is over the threshold")
    if loaded_ok:  # the best that is loaded is good enough; a load did not pay
        return Choice(best(loaded_ok), "use_loaded", "loaded and over the threshold")

    if profile.may_wait and profile.urgency != "now":
        return Choice(None, "wait", "nothing over the threshold is ready; the request waits")
    if rated_ok:
        pick = best(rated_ok)
        return Choice(pick, "fallback", "over the threshold, but not the best that is loaded")
    rated = [o for o in options if o.quality is not None]
    if rated:
        pick = best(rated)
        return Choice(pick, "fallback", "nothing over the threshold; the best that exists")
    return Choice(None, "none", "no option can answer this request")


# seconds a load may cost over the loaded best before the better model is no longer worth it
_LOAD_BUDGET = 60.0


# the rating a model needs to be "over the threshold" for an answer; until the thresholds per
# kind of work come from the bench (plan 6.5 (e)), one value for the answers of the chat
ANSWER_THRESHOLD = 0.6


def profile_for(work: str) -> Profile:
    """The profile of a kind of work: the chat answer waits for nobody, the rest may wait.
    Presets only; applications and tasks will carry their own profile later (plan 6.2)."""
    if work == "chat":
        return Profile(urgency="now", quality="good_enough", may_wait=False)
    return Profile(urgency="any", quality="good_enough", may_wait=True)


def first_choice(options: list[Option], profile: Profile) -> str | None:
    """The node the profile prefers, or None when the rules say nothing should move (waiting, no
    option, nothing rated). The caller keeps the old order in that case."""
    choice = choose(options, profile, ANSWER_THRESHOLD)
    if choice.option is None or choice.action in ("wait", "none"):
        return None
    return choice.option.name


def move_first(names: list[str], first: str | None) -> list[str]:
    """``names`` with ``first`` in front; the rest keep their order (they are the fallbacks)."""
    if first is None or first not in names:
        return names
    return [first, *(n for n in names if n != first)]


def first_option(options: list[Option], profile: Profile) -> Option | None:
    """The option the profile prefers, or None when nothing should move (see first_choice)."""
    choice = choose(options, profile, ANSWER_THRESHOLD)
    if choice.option is None or choice.action in ("wait", "none"):
        return None
    return choice.option


def order_pairs(
    pairs: list[tuple[str, str]],
    loaded: dict[tuple[str, str], bool],
    quality: dict[str, float],
    profile: Profile,
) -> list[tuple[str, str]]:
    """``(node, model)`` pairs with the one the profile prefers in front. The others keep their
    order as the fallbacks. Pairs without a rating are never preferred (choose() takes no
    unrated option over the threshold), so an unrated model keeps the old order."""
    options = [
        Option(
            name=node,
            model=model,
            loaded=loaded.get((node, model), False),
            quality=quality.get(model),
            seconds=None,
        )
        for node, model in pairs
    ]
    pick = first_option(options, profile)
    if pick is None:
        return pairs
    first = (pick.name, pick.model)
    return [first, *(p for p in pairs if p != first)] if first in pairs else pairs
