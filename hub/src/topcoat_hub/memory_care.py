"""Memory care (E3-05): how far a note can be trusted, how useful it has been, what the model
sees of that, when a note is checked, expires or is archived. Pure functions over a note's fields
(any object with the attributes of :class:`topcoat_hub.models.MemoryItem`) and a ``now`` — so the
rules are tested with a fake clock and the same code serves the recall, the care run and the page.

* **Reliability** = ``base x trust x 0.5^(age / half-life)``. ``base`` follows the evidence
  (``live`` > ``doc`` > ``assumption``), ``age`` runs from the last confirmation, check or creation.
  The half-life belongs to the kind of the note and grows with every confirmation (x1.5 each, at
  most x8); a live check counts double a user statement. A dispute lowers it strongly.
* **Usefulness** = a smoothed share of the times a note was used among the times it was put into a
  prompt (little data = neutral), the user's feedback weighing heavily, and the recency of the last
  use. Reliability and usefulness are separate: a note can be sure and never needed, or often needed
  and out of date (it is then *checked*, not dropped).
* **Rules**: age alone never deletes or archives. A note is archived only if usefulness is low,
  reliability is low *and* it has not been used for a long time."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from topcoat_hub.embedding import _fold
from topcoat_hub.knowledge_rules import keywords

KINDS = ("person", "preference", "operational")
EVIDENCE_BASE = {"live": 0.95, "doc": 0.8, "assumption": 0.5}
STAGES = ("sure", "older", "stale")

FEEDBACK_WEIGHT = 3.0  # one click of the user counts like this many recalls / uses
USE_PRIOR = 2.0  # (used + 2) / (recalled + 4): no data = 0.5, neutral
USE_HALFLIFE_DAYS = 120.0  # how fast the recency of the last use fades
DISPUTE_FACTOR = 0.3  # a note in dispute is trusted a third as much
UNVERIFIABLE_FACTOR = 0.7  # trust after a check that could not be made
TRUST_FLOOR = 0.2

# "used" = two signals agree: the model names the note and the answer shares its terms
OVERLAP_FULL = 0.5  # this share of the note's terms in the answer is the full overlap signal
USED_SCORE = 0.6  # 0.5 * model + 0.5 * overlap must reach this: neither signal decides alone
USED_OVERLAP_ALONE = 0.9  # no model signal at all (no extraction ran): only a very high overlap


@dataclass(frozen=True)
class CareConfig:
    """The tunable numbers (``TOPCOAT_MEMORY_*``)."""

    halflife_days: dict[str, float]
    volatile_halflife_days: float = 1.0
    # the operational notes of the Dev application are stale sooner
    dev_halflife_days: float = 7.0
    boost: float = 1.5
    boost_max: float = 8.0
    live_weight: float = 2.0
    stage_sure: float = 0.7
    stage_older: float = 0.35
    check_threshold: float = 0.5
    archive_usefulness: float = 0.3
    archive_reliability: float = 0.25
    archive_idle_days: float = 90.0
    volatile_ttl_hours: float = 24.0
    recheck_days: float = 3.0

    @classmethod
    def of(cls, s: Any) -> "CareConfig":
        return cls(
            halflife_days={
                "person": s.memory_halflife_person_days,
                "preference": s.memory_halflife_preference_days,
                "operational": s.memory_halflife_operational_days,
            },
            volatile_halflife_days=s.memory_halflife_volatile_days,
            dev_halflife_days=s.memory_halflife_dev_days,
            boost=s.memory_halflife_boost,
            boost_max=s.memory_halflife_boost_max,
            live_weight=s.memory_live_check_weight,
            stage_sure=s.memory_stage_sure,
            stage_older=s.memory_stage_older,
            check_threshold=s.memory_check_threshold,
            archive_usefulness=s.memory_archive_usefulness,
            archive_reliability=s.memory_archive_reliability,
            archive_idle_days=s.memory_archive_idle_days,
            volatile_ttl_hours=s.memory_volatile_ttl_hours,
            recheck_days=s.memory_care_recheck_days,
        )


DEFAULT = CareConfig(halflife_days={"person": 365.0, "preference": 180.0, "operational": 14.0})


def utcnow() -> datetime:
    """The clock of the care (tests replace it)."""
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _days(later: datetime, earlier: datetime) -> float:
    return max((later - earlier).total_seconds() / 86400.0, 0.0)


# --- reliability --------------------------------------------------------------------------------


def is_volatile(item: Any) -> bool:
    """A note that is only true for a short time (it carries an expiry)."""
    return getattr(item, "expires_at", None) is not None


def strength(item: Any, cfg: CareConfig = DEFAULT) -> float:
    """How often the note was confirmed, a live check counting ``live_weight`` times."""
    return (getattr(item, "confirmed_count", 0) or 0) + cfg.live_weight * (
        getattr(item, "verified_count", 0) or 0
    )


def halflife_days(item: Any, cfg: CareConfig = DEFAULT) -> float:
    """The half-life of this note in days: its kind's, lengthened by every confirmation. What Dev
    knows (an operational note) is shorter-lived than the kind says."""
    kind = getattr(item, "kind", "person") or "person"
    if is_volatile(item):
        base = cfg.volatile_halflife_days
    else:
        base = cfg.halflife_days.get(kind, 365.0)
        if getattr(item, "application", None) == "dev" and kind == "operational":
            base = min(base, cfg.dev_halflife_days)
    return base * min(cfg.boost ** strength(item, cfg), cfg.boost_max)


def reference_time(item: Any) -> datetime:
    """Where the age starts: the newest of creation, last confirmation and last check."""
    stamps = [
        _aware(getattr(item, name, None))
        for name in ("created_at", "last_confirmed_at", "last_verified_at")
    ]
    known = [s for s in stamps if s is not None]
    return max(known) if known else utcnow()


def reliability(item: Any, now: datetime | None = None, cfg: CareConfig = DEFAULT) -> float:
    """``base x trust x 0.5^(age / half-life)`` (x a third while the note is in dispute), 0..1."""
    now = _aware(now) or utcnow()
    base = EVIDENCE_BASE.get(getattr(item, "evidence", "doc"), 0.5)
    trust = min(max(getattr(item, "trust", 1.0) or 0.0, 0.0), 1.0)
    age = _days(now, reference_time(item))
    value = base * trust * 0.5 ** (age / halflife_days(item, cfg))
    if getattr(item, "status", "active") == "disputed":
        value *= DISPUTE_FACTOR
    return round(min(max(value, 0.0), 1.0), 4)


def stage(item: Any, now: datetime | None = None, cfg: CareConfig = DEFAULT) -> str:
    """``sure`` | ``older`` | ``stale``: the only form of reliability the model sees."""
    value = reliability(item, now, cfg)
    if value >= cfg.stage_sure:
        return "sure"
    if value >= cfg.stage_older:
        return "older"
    return "stale"


def needs_check(item: Any, now: datetime | None = None, cfg: CareConfig = DEFAULT) -> bool:
    """Is the note below the check threshold (and not checked very recently)?"""
    now = _aware(now) or utcnow()
    if getattr(item, "status", "active") != "active":
        return False
    last = _aware(getattr(item, "care_checked_at", None))
    if last is not None and _days(now, last) < cfg.recheck_days:
        return False
    return reliability(item, now, cfg) < cfg.check_threshold


def recheck_due(item: Any, now: datetime | None = None, cfg: CareConfig = DEFAULT) -> bool:
    """Was the note not looked at for ``recheck_days`` (or never)? Used for mirrored profiles with a
    check hint: their file is the truth, so no reliability threshold decides — the clock does."""
    now = _aware(now) or utcnow()
    last = _aware(getattr(item, "care_checked_at", None))
    return getattr(item, "status", "active") == "active" and (
        last is None or _days(now, last) >= cfg.recheck_days
    )


def is_expired(item: Any, now: datetime | None = None) -> bool:
    expires = _aware(getattr(item, "expires_at", None))
    return expires is not None and expires <= (_aware(now) or utcnow())


def expiry_for(now: datetime | None, cfg: CareConfig = DEFAULT) -> datetime | None:
    """When a volatile note expires (``None``: the TTL is 0, such a note is not stored at all)."""
    if cfg.volatile_ttl_hours <= 0:
        return None
    return (_aware(now) or utcnow()) + timedelta(hours=cfg.volatile_ttl_hours)


# --- usefulness ---------------------------------------------------------------------------------


def usefulness(item: Any, now: datetime | None = None) -> float:
    """0..1, 0.5 without data: ``(used + 2) / (recalled + 4)`` with the user's feedback weighing
    ``FEEDBACK_WEIGHT`` times, softened by how long ago the note was last used."""
    now = _aware(now) or utcnow()
    positive = getattr(item, "feedback_positive", 0) or 0
    negative = getattr(item, "feedback_negative", 0) or 0
    shown = (getattr(item, "recalled_count", 0) or 0) + FEEDBACK_WEIGHT * (positive + negative)
    used = (getattr(item, "used_count", 0) or 0) + FEEDBACK_WEIGHT * positive
    smoothed = (used + USE_PRIOR) / (shown + 2 * USE_PRIOR)
    last = _aware(getattr(item, "last_used_at", None))
    if (getattr(item, "used_count", 0) or 0) > 0 and last is not None:
        recency = 0.5 ** (_days(now, last) / USE_HALFLIFE_DAYS)
        smoothed *= 0.7 + 0.3 * recency
    return round(min(max(smoothed, 0.0), 1.0), 4)


def last_use_or_creation(item: Any) -> datetime:
    return _aware(getattr(item, "last_used_at", None)) or reference_creation(item)


def reference_creation(item: Any) -> datetime:
    return _aware(getattr(item, "created_at", None)) or utcnow()


def archive_due(item: Any, now: datetime | None = None, cfg: CareConfig = DEFAULT) -> bool:
    """Archive only if usefulness is low **and** reliability is low **and** the note has not been
    used for a long time (never used: since it was made). Age alone never archives."""
    now = _aware(now) or utcnow()
    if getattr(item, "status", "active") != "active":
        return False
    if usefulness(item, now) >= cfg.archive_usefulness:
        return False
    if reliability(item, now, cfg) >= cfg.archive_reliability:
        return False
    return _days(now, last_use_or_creation(item)) >= cfg.archive_idle_days


def rank_factor(item: Any, now: datetime | None = None, cfg: CareConfig = DEFAULT) -> float:
    """What the relevance of a hit is multiplied with in the recall: reliability and usefulness
    (1.0 for a fresh note without use data, a little over 1 for a note that proved useful)."""
    return (0.4 + 0.6 * reliability(item, now, cfg)) * (0.5 + usefulness(item, now))


# --- "used" -------------------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    return [_fold(w) for w in re.findall(r"\w+", text.lower()) if len(w) >= 3]


def overlap(note: str, answer: str) -> float:
    """The share (0..1) of the note's content words that occur in the answer (a word and a longer
    form of it, such as a plural, match)."""
    mine = [_fold(w) for w in keywords(note) if len(w) >= 3]
    if not mine:
        return 0.0
    theirs = set(_tokens(answer))

    def present(word: str) -> bool:
        if word in theirs:
            return True
        stem = word[:5]
        return len(word) >= 5 and any(t.startswith(stem) for t in theirs if len(t) >= 5)

    return sum(1 for w in set(mine) if present(w)) / len(set(mine))


def counts_as_used(model_named: bool | None, overlap_ratio: float) -> bool:
    """Two signals decide, never one: the model named the note (``None`` = no answer of the model
    about it) and the answer shares its terms. Small models report unreliably, term overlap
    alone is only a guess — together they are good enough."""
    scaled = min(overlap_ratio / OVERLAP_FULL, 1.0)
    if model_named is None:
        return scaled >= USED_OVERLAP_ALONE
    return 0.5 * (1.0 if model_named else 0.0) + 0.5 * scaled >= USED_SCORE


# --- what the model sees -------------------------------------------------------------------------


def verify_input_ok(value: Any) -> dict[str, Any] | None:
    """A ``verify_with`` the model proposed, as far as it can be trusted to be one:
    ``{"capability": "name.like.this", "input": {…small…}}``; else ``None``. Whether the capability
    exists and is read-only is decided by the care run against what the user may run."""
    if not isinstance(value, dict):
        return None
    cap = value.get("capability")
    if not isinstance(cap, str) or not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", cap):
        return None
    given = value.get("input", {})
    if not isinstance(given, dict) or len(str(given)) > 400:
        return None
    return {"capability": cap, "input": given}


def kind_of(value: Any) -> str:
    return value if value in KINDS else "person"


def kinds(items: Iterable[Any]) -> dict[str, int]:
    counts = dict.fromkeys(KINDS, 0)
    for item in items:
        counts[kind_of(getattr(item, "kind", "person"))] += 1
    return counts
