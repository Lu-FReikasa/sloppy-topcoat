"""Memory care (E3-05), the rules without a database: reliability and its half-life, the stages
the model sees, usefulness and "used", the check threshold, expiry, archive, the plan of a care run
and the night boundary — all with an explicit ``now`` (a fake clock)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from topcoat_hub import chat
from topcoat_hub import memory_care as mc
from topcoat_hub.config import Settings
from topcoat_hub.memory_care_service import night_boundary, plan_for, vet_verdict

NOW = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)


def days(n: float) -> datetime:
    return NOW - timedelta(days=n)


def note(**kw: Any) -> SimpleNamespace:
    """A note as the rules see it (any object with the attributes of a MemoryItem)."""
    base: dict[str, Any] = {
        "id": kw.pop("id", object()),
        "text": "Der Server node-amd läuft mit Docker",
        "status": "active",
        "evidence": "doc",
        "kind": "person",
        "trust": 1.0,
        "created_at": NOW,
        "last_confirmed_at": None,
        "last_verified_at": None,
        "confirmed_count": 0,
        "verified_count": 0,
        "expires_at": None,
        "verify_with": None,
        "care_checked_at": None,
        "recalled_count": 0,
        "used_count": 0,
        "last_used_at": None,
        "feedback_positive": 0,
        "feedback_negative": 0,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# --- reliability: base x trust x 0.5^(age / half-life) ----------------------------------------


def test_reliability_halves_every_half_life_of_the_kind() -> None:
    fresh = mc.reliability(note(created_at=NOW), NOW)
    assert fresh == pytest.approx(0.8)  # [doc]
    one = mc.reliability(note(created_at=days(365)), NOW)  # person: 365 d
    two = mc.reliability(note(created_at=days(730)), NOW)
    assert one == pytest.approx(0.4, abs=1e-3)
    assert two == pytest.approx(0.2, abs=1e-3)
    # an operational note (14 d) has lost the same after two weeks
    assert mc.reliability(note(kind="operational", created_at=days(14)), NOW) == pytest.approx(
        0.4, abs=1e-3
    )
    # a preference (180 d) lies in between
    assert mc.reliability(note(kind="preference", created_at=days(180)), NOW) == pytest.approx(
        0.4, abs=1e-3
    )


def test_defaults_are_the_ones_of_the_order() -> None:
    assert mc.DEFAULT.halflife_days == {"person": 365.0, "preference": 180.0, "operational": 14.0}
    assert mc.DEFAULT.volatile_halflife_days == 1.0
    assert (mc.DEFAULT.boost, mc.DEFAULT.boost_max, mc.DEFAULT.live_weight) == (1.5, 8.0, 2.0)
    assert mc.DEFAULT.volatile_ttl_hours == 24.0
    # a volatile note (it has an expiry) decays with its own, short half-life
    volatile = note(kind="operational", expires_at=NOW + timedelta(hours=24), created_at=days(1))
    assert mc.halflife_days(volatile) == 1.0
    assert mc.reliability(volatile, NOW) == pytest.approx(0.4, abs=1e-3)


def test_the_base_follows_the_kind_of_evidence() -> None:
    live, doc, guess = (
        mc.reliability(note(evidence=e), NOW) for e in ("live", "doc", "assumption")
    )
    assert live > doc > guess


def test_every_confirmation_resets_the_clock_and_lengthens_the_half_life() -> None:
    old = note(kind="operational", created_at=days(40))
    confirmed = note(kind="operational", created_at=days(40), last_confirmed_at=days(2))
    assert mc.reliability(confirmed, NOW) > mc.reliability(old, NOW) * 5  # clock restarted
    # half-life x1.5 per confirmation, at most x8
    hl = [mc.halflife_days(note(kind="operational", confirmed_count=n)) for n in (0, 1, 2, 3, 10)]
    assert hl[0] == 14.0
    assert hl[1] == pytest.approx(21.0)
    assert hl[2] == pytest.approx(31.5)
    assert hl[3] == pytest.approx(47.25)
    assert hl[4] == pytest.approx(14.0 * 8)  # capped
    # the same age is worth more trust the more often it was confirmed
    a = mc.reliability(note(kind="operational", created_at=days(20)), NOW)
    b = mc.reliability(note(kind="operational", created_at=days(20), confirmed_count=3), NOW)
    assert b > a


def test_a_live_check_counts_more_than_a_statement() -> None:
    by_check = note(kind="operational", verified_count=1)
    by_words = note(kind="operational", confirmed_count=1)
    assert mc.halflife_days(by_check) > mc.halflife_days(by_words)
    assert mc.halflife_days(by_check) == mc.halflife_days(
        note(kind="operational", confirmed_count=2)
    )
    # and a check restarts the clock like a confirmation does
    checked = note(kind="operational", created_at=days(60), last_verified_at=days(1))
    assert mc.reference_time(checked) == days(1)
    assert mc.reliability(checked, NOW) > 0.5


def test_a_dispute_lowers_the_trust_strongly_and_an_unverifiable_check_a_little() -> None:
    plain = mc.reliability(note(), NOW)
    assert mc.reliability(note(status="disputed"), NOW) == pytest.approx(plain * 0.3, abs=1e-3)
    assert mc.reliability(note(trust=0.7), NOW) == pytest.approx(plain * 0.7, abs=1e-3)
    assert mc.UNVERIFIABLE_FACTOR == 0.7 and mc.TRUST_FLOOR == 0.2


def test_the_model_sees_three_stages_never_numbers() -> None:
    assert mc.stage(note(evidence="live"), NOW) == "sure"
    assert mc.stage(note(kind="operational", created_at=days(14)), NOW) == "older"  # 0.4
    assert mc.stage(note(kind="operational", created_at=days(60)), NOW) == "stale"
    hints = {s: chat.note_hint("doc", "active", 0, s) for s in ("sure", "older", "stale")}
    assert "older and not confirmed again" in hints["older"]
    assert "possibly outdated" in hints["stale"]
    for hint in (*hints.values(), chat.note_hint("doc", "active", 3, "ask")):
        assert not any(ch.isdigit() for ch in hint), hint
    assert "ask once, in passing" in chat.note_hint("doc", "active", 0, "ask")


# --- when a note is checked, expires, is archived ---------------------------------------------


def test_a_note_is_checked_below_the_threshold_and_not_again_right_away() -> None:
    fresh = note(kind="operational", created_at=days(1))
    stale = note(kind="operational", created_at=days(30))
    assert not mc.needs_check(fresh, NOW)
    assert mc.needs_check(stale, NOW)
    # just checked: left alone for the recheck period
    assert not mc.needs_check(
        note(kind="operational", created_at=days(30), care_checked_at=days(1)), NOW
    )
    assert mc.needs_check(
        note(kind="operational", created_at=days(30), care_checked_at=days(4)), NOW
    )
    # only live notes are looked at
    assert not mc.needs_check(note(kind="operational", created_at=days(30), status="archived"), NOW)
    # the threshold is a setting
    strict = mc.CareConfig(halflife_days=mc.DEFAULT.halflife_days, check_threshold=0.9)
    assert mc.needs_check(note(created_at=days(1)), NOW, strict)


def test_a_volatile_note_expires_at_expires_at_and_never_before() -> None:
    until = mc.expiry_for(NOW)
    assert until == NOW + timedelta(hours=24)
    volatile = note(expires_at=until)
    assert not mc.is_expired(volatile, NOW + timedelta(hours=23, minutes=59))
    assert mc.is_expired(volatile, NOW + timedelta(hours=24))
    assert not mc.is_expired(note(), NOW + timedelta(days=9999))  # no expiry, no expiry
    off = mc.CareConfig(halflife_days=mc.DEFAULT.halflife_days, volatile_ttl_hours=0)
    assert mc.expiry_for(NOW, off) is None  # TTL 0: such a note is not stored at all


def test_usefulness_is_neutral_without_data_and_moves_with_use_and_feedback() -> None:
    assert mc.usefulness(note(), NOW) == 0.5
    often = mc.usefulness(note(recalled_count=20, used_count=18, last_used_at=days(1)), NOW)
    never = mc.usefulness(note(recalled_count=20, used_count=0), NOW)
    assert often > 0.75 and never < 0.2
    # a little data does not move it far (smoothing)
    assert 0.35 < mc.usefulness(note(recalled_count=1, used_count=0), NOW) < 0.5
    # the user's word counts heavily, and "did not fit" cannot be outvoted by a few recalls
    assert mc.usefulness(note(feedback_negative=2, recalled_count=3, used_count=2), NOW) < 0.35
    assert mc.usefulness(note(feedback_positive=2), NOW) > 0.7
    # the more recent the last use, the better
    recent = mc.usefulness(note(recalled_count=10, used_count=8, last_used_at=days(2)), NOW)
    long_ago = mc.usefulness(note(recalled_count=10, used_count=8, last_used_at=days(600)), NOW)
    assert recent > long_ago


def test_age_alone_never_archives() -> None:
    ancient = note(created_at=days(5000))  # never recalled, never used: usefulness is neutral
    assert mc.reliability(ancient, NOW) < 0.01
    assert mc.usefulness(ancient, NOW) == 0.5
    assert not mc.archive_due(ancient, NOW)
    # a note that was shown often and never helped, but is reliable (just confirmed): kept
    confirmed = note(created_at=days(200), last_confirmed_at=days(1), recalled_count=30)
    assert mc.usefulness(confirmed, NOW) < 0.3
    assert not mc.archive_due(confirmed, NOW)
    # useless and unreliable, but used a week ago: kept
    recent = note(created_at=days(5000), recalled_count=30, last_used_at=days(7), used_count=1)
    assert not mc.archive_due(recent, NOW)


def test_a_note_is_archived_only_when_useless_unreliable_and_unused_for_long() -> None:
    useless = note(created_at=days(5000), recalled_count=30)
    assert mc.usefulness(useless, NOW) < 0.3
    assert mc.reliability(useless, NOW) < 0.25
    assert mc.archive_due(useless, NOW)
    # one day short of the period it still stays (never used: counted from its creation)
    young = note(created_at=days(89), recalled_count=30, kind="operational")
    assert mc.reliability(young, NOW) < 0.25 and mc.usefulness(young, NOW) < 0.3
    assert not mc.archive_due(young, NOW)
    assert mc.archive_due(note(created_at=days(91), recalled_count=30, kind="operational"), NOW)
    # the user said it did not fit
    assert mc.archive_due(note(created_at=days(5000), feedback_negative=3, recalled_count=2), NOW)
    # only live notes are archived
    assert not mc.archive_due(note(created_at=days(5000), recalled_count=30, status="deleted"), NOW)


def test_an_old_note_that_is_often_used_stays_and_is_only_checked() -> None:
    old = note(
        kind="operational",
        created_at=days(400),
        recalled_count=45,
        used_count=40,
        last_used_at=days(3),
        verify_with={"capability": "docker.ps", "input": {}},
    )
    assert mc.reliability(old, NOW) < 0.01
    assert mc.usefulness(old, NOW) > 0.8
    assert not mc.archive_due(old, NOW)
    plan = plan_for([old], [], NOW, mc.DEFAULT)
    assert plan.checks == [old] and plan.archive == []


def test_recall_ranks_by_reliability_and_never_by_use_counts() -> None:
    """D124: how often a note was recalled or used is no ranking input; reliability is."""
    fresh = note(created_at=days(1), kind="operational")
    old = note(created_at=days(90), kind="operational")
    counted = note(
        created_at=days(1),
        kind="operational",
        recalled_count=10,
        used_count=9,
        last_used_at=days(1),
        feedback_positive=3,
    )
    f, o, c = (mc.rank_factor(n, NOW) for n in (fresh, old, counted))
    assert c == f > o
    assert mc.rank_factor(note(created_at=days(1), kind="operational", recalled_count=25), NOW) == f
    # an old note without any data is not buried
    assert mc.rank_factor(note(created_at=days(2000)), NOW) > 0.15


# --- "used": two signals, never one ---------------------------------------------------------


def test_a_note_counts_as_used_only_when_two_signals_agree() -> None:
    full = 0.8  # most of the note's terms are in the answer
    assert mc.counts_as_used(True, full)
    assert not mc.counts_as_used(True, 0.0)  # the model says so, the answer does not show it
    assert not mc.counts_as_used(False, full)  # the answer shares terms, the model says no
    assert mc.counts_as_used(True, 0.3)  # a partial overlap plus the model's word
    # no answer of the model about it (the extraction did not run): only a very high overlap
    assert not mc.counts_as_used(None, 0.3)
    assert mc.counts_as_used(None, 0.5)


def test_term_overlap_matches_forms_of_a_word_and_ignores_filler() -> None:
    answer = "Du wohnst in Hamburg und hast zwei Hunde namens Bello und Rex."
    assert mc.overlap("Wohnt in Hamburg", answer) >= 0.5
    assert mc.overlap("Hat einen Hund namens Bello", answer) >= 0.5  # Hund / Hunde
    assert mc.overlap("Trinkt gern Kaffee", answer) == 0.0
    assert mc.overlap("", answer) == 0.0


# --- what the model may propose, what a care run does with it ------------------------------------


@pytest.mark.parametrize(
    "value, ok",
    [
        ({"capability": "docker.ps", "input": {}}, True),
        ({"capability": "kuma.monitors", "input": {"name": "web"}}, True),
        ({"capability": "docker", "input": {}}, False),  # not a capability name
        ({"capability": "rm -rf /", "input": {}}, False),
        ({"capability": "docker.ps", "input": "x"}, False),
        ({"capability": "docker.ps", "input": {"a": "x" * 500}}, False),
        ("docker.ps", False),
        (None, False),
    ],
)
def test_a_verify_with_must_look_like_a_capability_and_a_small_input(value: Any, ok: bool) -> None:
    assert (mc.verify_input_ok(value) is not None) is ok


def test_the_plan_sorts_notes_into_what_the_run_does() -> None:
    ps = {"capability": "docker.ps", "input": {}}
    doomed = note(kind="operational", expires_at=NOW - timedelta(hours=1))
    pending = note(kind="operational", expires_at=NOW - timedelta(hours=1), created_at=days(3))
    checkable = note(kind="operational", created_at=days(40), verify_with=ps)
    blind = note(kind="operational", created_at=days(40))  # nothing to run
    person = note(kind="person", created_at=days(900))  # nothing a server can show
    volatile = note(kind="operational", created_at=days(3), expires_at=NOW + timedelta(hours=2))
    in_notes = note(
        kind="operational",
        created_at=days(40),
        verify_with={"capability": "docs.read", "input": {"path": "servers/SENSIBEL/x.md"}},
    )
    on_web = note(
        kind="operational",
        created_at=days(40),
        verify_with={"capability": "web.fetch", "input": {"url": "https://example.org"}},
    )
    fine = note(kind="operational", created_at=days(1), verify_with=ps)
    useless = note(kind="preference", created_at=days(5000), recalled_count=30)
    plan = plan_for(
        [pending, checkable, blind, person, volatile, in_notes, on_web, fine, useless],
        [doomed],
        NOW,
        mc.DEFAULT,
    )
    assert plan.expired == [doomed]
    assert plan.checks == [checkable]
    assert plan.unverifiable == [blind, in_notes, on_web]  # the notes and the web are never used
    assert plan.archive == [useless]
    assert plan.skipped == 3  # person, volatile, and the pending one (expires, not checked)
    assert plan.has_work
    assert not plan_for([fine], [], NOW, mc.DEFAULT).has_work


def test_the_most_doubtful_note_is_checked_first() -> None:
    ps = {"capability": "docker.ps", "input": {}}
    a = note(kind="operational", created_at=days(20), verify_with=ps)
    b = note(kind="operational", created_at=days(90), verify_with=ps)
    c = note(kind="operational", created_at=days(45), verify_with=ps)
    assert plan_for([a, b, c], [], NOW, mc.DEFAULT).checks == [b, c, a]


# --- the verdict of the judge ---------------------------------------------------------------------


def test_a_verdict_is_vetted_before_it_is_believed() -> None:
    ok = vet_verdict({"verdict": "confirmed", "evidence": ["E1", 7], "reason": " fine  here "})
    assert ok == {
        "verdict": "confirmed",
        "text": "",
        "quote": "",
        "evidence": ["E1"],
        "reason": "fine here",
    }
    changed = vet_verdict({"verdict": "changed", "text": "Port 8081", "evidence": ["E1"]})
    assert changed is not None and changed["text"] == "Port 8081"
    for bad in (
        None,
        "confirmed",
        {"verdict": "maybe"},
        {"verdict": "changed", "evidence": ["E1"]},  # no corrected text
        {"verdict": "changed", "text": "x" * 400, "evidence": ["E1"]},  # not a note
        {"verdict": "changed", "text": "Das Passwort ist hunter2x", "evidence": ["E1"]},  # secret
    ):
        assert vet_verdict(bad) is None  # type: ignore[arg-type]


# --- when a run is due ----------------------------------------------------------------------------


def test_the_night_boundary_is_the_latest_care_hour_not_after_now() -> None:
    at_two = datetime(2026, 10, 1, 2, 0, tzinfo=UTC)
    at_four = datetime(2026, 10, 1, 4, 30, tzinfo=UTC)
    assert night_boundary(at_four, 3, UTC) == datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
    assert night_boundary(at_two, 3, UTC) == datetime(2026, 9, 30, 3, 0, tzinfo=UTC)
    assert night_boundary(at_four, 0, UTC) == datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    assert night_boundary(at_four, -1, UTC) is None  # no fixed hour: only after idle
    assert night_boundary(at_four, 24, UTC) is None


def test_the_settings_reach_the_rules() -> None:
    s = Settings(
        memory_halflife_operational_days=7,
        memory_check_threshold=0.6,
        memory_archive_idle_days=30,
        memory_volatile_ttl_hours=12,
    )
    cfg = mc.CareConfig.of(s)
    assert cfg.halflife_days["operational"] == 7
    assert (cfg.check_threshold, cfg.archive_idle_days, cfg.volatile_ttl_hours) == (0.6, 30, 12)
    assert mc.halflife_days(note(kind="operational"), cfg) == 7


# --- mirrored profiles with a check hint --------------------------------------------------------


def profile(host: str = "alpha", hint: str = "`occ status`", **kw: Any) -> SimpleNamespace:
    return note(
        kind="operational",
        source_type="kb",
        source_ref={"host": host, "check_hint": hint},
        evidence="doc",
        created_at=days(2),
        **kw,
    )


def test_a_profile_is_checked_when_its_host_has_a_node_and_left_alone_when_not() -> None:
    near, far = profile("alpha"), profile("charlie")
    plan = plan_for([near, far], [], NOW, mc.DEFAULT, {"alpha": "tools-alpha"})
    assert plan.checks == [near]
    assert plan.no_node == 1 and plan.skipped == 0  # no error, no failed check
    # more nodes later widen the run without any other change
    both = plan_for(
        [near, far], [], NOW, mc.DEFAULT, {"alpha": "tools-alpha", "charlie": "tools-charlie"}
    )
    assert both.checks == [near, far]


def test_a_profile_is_rechecked_only_after_the_recheck_days() -> None:
    reach = {"alpha": "tools-alpha"}
    fresh = profile(care_checked_at=days(1))
    stale = profile(care_checked_at=days(4))
    plan = plan_for([fresh, stale], [], NOW, mc.DEFAULT, reach)
    assert plan.checks == [stale] and plan.no_node == 0


def test_a_profile_without_a_hint_is_not_checked() -> None:
    bare = profile(hint="")
    plan = plan_for([bare], [], NOW, mc.DEFAULT, {"alpha": "tools-alpha"})
    assert plan.checks == [] and not plan.has_work


def test_reachable_hosts_come_from_the_nodes_that_offer_the_shell() -> None:
    from topcoat_hub.memory_care_service import reachable_hosts

    available = [
        {"node": "tools-alpha", "capability": "shell.read"},
        {"node": "tools-bravo", "capability": "docker.ps"},  # no shell: no host
        {"node": "delta", "capability": "shell.read"},
    ]
    assert reachable_hosts(available) == {"alpha": "tools-alpha", "delta": "delta"}


def test_a_care_check_may_wait_for_a_slow_model() -> None:
    from topcoat_hub.memory_care_service import MemoryCare

    care = MemoryCare.__new__(MemoryCare)  # only the settings matter for the limits
    care.settings = Settings(
        memory_care_model_timeout_seconds=777.0, memory_care_task_seconds=1500.0
    )
    limits = care.limits()
    assert limits.model_timeout_seconds == 777.0
    assert limits.time_budget_seconds == 1500.0
    # the defaults let a 30B model on a CPU laptop read a prompt (it took more than 2 minutes)
    default = Settings()
    assert default.memory_care_model_timeout_seconds >= 600
    assert default.memory_care_run_max_seconds >= default.memory_care_task_seconds


def test_a_difference_needs_a_quote_that_stands_in_a_non_empty_answer() -> None:
    from topcoat_hub.memory_care_service import is_empty_answer, quote_proves

    seen = SimpleNamespace(excerpt='{"containers":[{"name":"web","ports":"0.0.0.0:8081->80/tcp"}]}')
    empty = SimpleNamespace(excerpt='{"containers":[],"counts":{},"total":0}')
    assert quote_proves("8081->80/tcp", [seen])
    assert quote_proves("  8081->80/TCP ", [seen])  # case and spaces do not matter
    assert not quote_proves("8082", [seen])  # not in the answer
    assert not quote_proves("", [seen])
    assert not quote_proves("ab", [seen])  # too short to prove anything
    assert not quote_proves("containers", [empty])  # found, but an empty answer shows nothing
    assert is_empty_answer('{"containers":[],"total":0}')
    assert is_empty_answer("")
    assert not is_empty_answer('{"total":2}')
