"""The core and the personas as pure functions (E3-06): budget and cut order, the stamp, rendering
of a persona within its tokens, the questionnaire, the parser of the infrastructure profiles and the
review of the core. No database, no clock but the one passed in."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from topcoat_hub import core_mirror, core_review, core_rules, memory_care, personas

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def item(text: str, kind: str = "person", **kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "text": text,
        "kind": kind,
        "core": False,
        "weak": False,
        "example": None,
        "status": "active",
        "evidence": "doc",
        "trust": 1.0,
        "confirmed_count": 0,
        "verified_count": 0,
        "recalled_count": 0,
        "used_count": 0,
        "feedback_positive": 0,
        "feedback_negative": 0,
        "created_at": NOW - timedelta(days=5),
        "last_confirmed_at": None,
        "last_verified_at": None,
        "last_used_at": None,
        "expires_at": None,
        "version": 1,
    }
    return SimpleNamespace(**{**base, **kw})


# --- the budget -------------------------------------------------------------------------------


def test_the_budget_is_1000_tokens_and_500_for_a_context_below_8k() -> None:
    assert core_rules.budget_for(16384, 1000) == 1000
    assert core_rules.budget_for(None, 1000) == 1000
    assert core_rules.budget_for(8192, 1000) == 1000
    assert core_rules.budget_for(4096, 1000) == 500
    assert core_rules.budget_for(4096, 300) == 300  # a smaller configured value stays
    assert core_rules.tokens("x" * 30) == 10 + core_rules.OVERHEAD_TOKENS  # 3 characters a token


def test_the_block_stays_within_its_budget_at_8k_and_16k_whatever_is_thrown_at_it() -> None:
    many = lambda kind, n: [  # noqa: E731
        item(f"{kind} number {i} says something rather long to fill the room " * 2, kind)
        for i in range(n)
    ]
    for context, budget in ((8192, 1000), (16384, 1000), (4096, 500)):
        assert core_rules.budget_for(context, 1000) == budget
        block = core_rules.build(
            system=["Stick to evidence. " * 12],
            rules=many("rule", 30),
            profile=many("person", 60),
            household=many("operational", 40),
            persona="Be brief. " * 100,
            budget=budget,
            now=NOW,
        )
        body = block.total - block.used.get("recent", 0)
        assert body <= budget + 10, (context, block.used)  # (each part's heading counts)


def test_the_cut_order_is_profile_then_household_then_examples_then_rules_never_the_system() -> (
    None
):
    system = ["S" * 600]  # longer than its share: the block is over budget, something must go
    rules = [item(f"A rule that is rather long, number {i}. " * 3, "rule") for i in range(3)]
    style = item("Short and dry.", "style", example="Done. Nothing broke.")
    profile = [item(f"Profile fact number {i} is long enough to matter.") for i in range(6)]
    household = [
        item(f"Household fact {i} is also quite long enough.", "operational") for i in range(6)
    ]
    everything = [*profile, *household, *rules, style]
    seen_examples_dropped = False
    for budget in range(700, 0, -10):
        b = core_rules.build(
            system=system, rules=[*rules, style], profile=profile, household=household,
            budget=budget, now=NOW,
        )  # fmt: skip
        assert "S" * 600 in b.text  # the system rules are never cut, whatever the budget
        kinds = [
            "profile" if i in profile else "household" if i in household else "rules"
            for i in (next(x for x in everything if str(x.id) == c) for c in b.cut)
        ]
        assert kinds == sorted(kinds, key={"profile": 0, "household": 1, "rules": 2}.get), budget
        if "household" in kinds:  # the household only goes when the profile is gone
            assert b.included["profile"] == [], budget
        if b.examples_dropped:  # examples only go when profile and household are gone
            assert b.included["profile"] == [] and b.included["household"] == [], budget
            seen_examples_dropped = True
        if "rules" in kinds:  # a rule only goes after the examples did
            assert b.examples_dropped, budget
    assert seen_examples_dropped
    full = core_rules.build(
        system=["S" * 20], rules=rules, profile=profile, household=household, budget=10_000, now=NOW
    )
    assert full.cut == [] and not full.examples_dropped


def test_use_counts_do_not_move_a_note_in_the_core() -> None:
    """D124: recalled and used counts and the feedback are no ranking input."""
    plain = item("plain", created_at=NOW - timedelta(days=3))
    counted = item(
        "counted",
        created_at=NOW - timedelta(days=3),
        recalled_count=40,
        used_count=30,
        feedback_positive=5,
        last_used_at=NOW,
    )
    order = core_rules.ranked([counted, plain], NOW, memory_care.DEFAULT)
    assert order == core_rules.ranked([plain, counted], NOW, memory_care.DEFAULT)
    assert core_rules._score(plain, NOW, memory_care.DEFAULT) == core_rules._score(
        counted, NOW, memory_care.DEFAULT
    )


def test_pinned_comes_first_then_reliability() -> None:
    fresh = item("fresh", created_at=NOW - timedelta(days=1), confirmed_count=3)
    stale = item("stale", created_at=NOW - timedelta(days=900))
    pinned = item("pinned but stale", core=True, created_at=NOW - timedelta(days=900))
    weak = item("weak hint", "style", weak=True, core=False)
    order = core_rules.ranked([stale, weak, fresh, pinned], NOW, memory_care.DEFAULT)
    assert order[0] is pinned and order.index(fresh) < order.index(stale) < order.index(weak)
    # the same input, the same order, every time
    assert core_rules.ranked([weak, pinned, stale, fresh], NOW, memory_care.DEFAULT) == order


def test_a_style_with_an_example_is_printed_with_it_and_adjectives_alone_are_a_weak_hint() -> None:
    with_example = item("Short.", "style", example='Done. "Nothing broke."')
    block = core_rules.build(system=["x"], rules=[with_example], profile=[], household=[],
                             budget=1000, now=NOW)  # fmt: skip
    assert 'Example: "Done. "Nothing broke.""' in block.text
    assert core_rules.is_weak_style("freundlich, knapp", None)
    assert core_rules.is_weak_style("friendly and short", None)
    assert not core_rules.is_weak_style("freundlich, knapp", "Alles läuft.")
    assert not core_rules.is_weak_style(
        "Beginne jede Antwort mit dem Ergebnis in einem Satz.", None
    )
    weak = item("freundlich, knapp", "style", weak=True)
    assert (
        "(a hint only)"
        in core_rules.build(
            system=["x"], rules=[weak], profile=[], household=[], budget=1000, now=NOW
        ).text
    )
    assert core_rules.clean_example("  a   b \n c  ") == "a b c"
    assert len(core_rules.clean_example("x" * 1000) or "") <= core_rules.EXAMPLE_MAX_CHARS


def chat(n: int, statements: list[str], title: str = "") -> core_rules.RecentChat:
    return core_rules.RecentChat(
        id=f"c{n}", title=title or f"Chat {n}", day="2026-11-03", last_message_id=n,
        statements=tuple(statements),
    )  # fmt: skip


def test_the_latest_chats_have_their_own_150_tokens_and_the_last_two_only() -> None:
    chats = [
        chat(i, [f"Term {i}-{k}: " + "what was settled and what is open. " * 3 for k in range(5)])
        for i in range(4)
    ]
    block = core_rules.build(
        system=["x"], rules=[], profile=[], household=[], recent=chats, budget=1000, now=NOW
    )
    assert block.included["recent"] == ["c0", "c1"]
    assert block.used["recent"] <= 150 + core_rules.OVERHEAD_TOKENS
    assert "## Discussed last" in block.text


def test_a_chat_is_shown_as_key_terms_the_newest_first_to_fit_in_the_order_they_were_made() -> None:
    statements = [
        "Backup-Job: läuft nachts um 3 Uhr",
        "Ziel: noch offen, NAS oder Cloud",
        "Rotation: 7 Tage behalten",
    ]
    text, ids, spent = core_rules._recent([chat(1, statements, "Backup")], 150)
    assert ids == ["c1"] and spent > 0
    assert "- Backup (2026-11-03): " in text
    assert "Backup-Job: läuft nachts um 3 Uhr; Ziel: noch offen, NAS oder Cloud; Rotation" in text
    # little room: the newest statements stay, the oldest go
    short, _, _ = core_rules._recent([chat(1, statements, "Backup")], 70)
    assert "Rotation: 7 Tage behalten" in short and "Backup-Job" not in short


def test_a_chat_that_does_not_fit_is_left_out_whole_and_no_chat_means_no_section() -> None:
    assert core_rules._recent([], 150) == ("", [], 0)
    big = chat(1, ["X" * 400])
    text, ids, _ = core_rules._recent([big], 40)
    assert "…" in text or ids == []  # a long statement is cut, never put in whole
    assert core_rules._recent([chat(2, [])], 150) == ("", [], 0)


def test_the_stamp_changes_with_the_core_not_with_time() -> None:
    args: dict[str, Any] = {
        "budget": 1000, "language": "de", "memory_enabled": True, "system": [("s", 1)],
        "rules": [("r", 1, True)], "pinned": [], "household": [], "recent": [],
    }  # fmt: skip
    base = core_rules.stamp(**args)
    assert core_rules.stamp(**args) == base
    assert core_rules.stamp(**{**args, "rules": [("r", 2, True)]}) != base  # a core entry changed
    assert core_rules.stamp(**{**args, "language": "en"}) != base
    assert core_rules.stamp(**{**args, "budget": 500}) != base
    assert core_rules.stamp(**{**args, "persona": "abc@2"}) != base  # a change of persona


# --- personas ---------------------------------------------------------------------------------


def template(key: str) -> dict[str, Any]:
    return personas._template_data(key)


def test_all_templates_render_in_german_and_english_within_400_tokens() -> None:
    keys = [t["key"] for t in personas.library()["templates"]]
    assert keys == ["butler", "friend", "copilot", "neutral", "clipper"]
    for key in keys:
        t = template(key)
        for lang in ("de", "en"):
            r = personas.render(t["components"], t["example"], lang)
            assert r.text and r.tokens <= personas.MAX_TOKENS and not r.truncated, (key, lang)
            assert t["example"][lang] in r.text  # the fixed example sentence belongs to the text
    butler = personas.render(template("butler")["components"], template("butler")["example"], "de")
    assert "Haus-Assistent" in butler.text and "Humor" in butler.text
    assert "lass den Humor immer weg" in butler.text  # humor is always off for errors and risk
    neutral = personas.render(
        template("neutral")["components"], template("neutral")["example"], "de"
    )
    assert "Kein Humor" in neutral.text and "Humor ist willkommen" not in neutral.text


def test_humor_is_off_for_errors_and_risk_at_every_level_but_zero() -> None:
    for level in (1, 2):
        comp = {**template("friend")["components"], "humor": level}
        for lang in ("de", "en"):
            text = personas.render(comp, {}, lang).text
            assert ("lass den Humor immer weg" if lang == "de" else "leave the humor out") in text


def test_a_persona_over_its_tokens_loses_the_example_first_and_the_role_last() -> None:
    comp = {**template("copilot")["components"]}
    full = personas.render(comp, template("copilot")["example"], "de")
    assert template("copilot")["example"]["de"] in full.text
    cut = personas.render(comp, template("copilot")["example"], "de", max_tokens=full.tokens - 5)
    assert cut.truncated and template("copilot")["example"]["de"] not in cut.text
    assert "Co-Pilot" in cut.text  # the role stays
    tiny = personas.render(comp, template("copilot")["example"], "de", max_tokens=30)
    assert tiny.truncated and tiny.tokens <= 30 and tiny.text.startswith("Du bist mein Co-Pilot")


def test_a_free_text_is_cut_at_a_sentence_to_400_tokens_and_says_so() -> None:
    text = " ".join(f"Satz Nummer {i} ist hier." for i in range(400))
    r = personas.render({"free_text": text}, None, "de")
    assert r.truncated and r.tokens <= personas.MAX_TOKENS and r.text.endswith(".")
    short = personas.render({"free_text": "Antworte kurz."}, None, "de")
    assert short.text == "Antworte kurz." and not short.truncated


def test_the_same_blocks_give_the_same_text_byte_for_byte() -> None:
    t = template("friend")
    a = personas.render(t["components"], t["example"], "de").text
    b = personas.render(
        dict(reversed(list(t["components"].items()))), dict(t["example"]), "de"
    ).text
    assert a == b


def test_components_are_checked_and_a_secret_is_refused() -> None:
    ok = personas.clean_components(
        {"role": {"de": "Du bist ruhig."}, "address": "sie", "humor": "2", "emojis": "true"}
    )
    assert ok["humor"] == 2 and ok["emojis"] is True and ok["address"] == "sie"
    for bad in (
        {"role": {"de": "x"}, "humor": 7},
        {"role": {"de": "x"}, "length": "endless"},
        {"role": {"de": "x"}, "ask": "never"},
        {"role": {"de": "x"}, "address": "ihr"},
        {"address": "du"},  # no role
        "nonsense",
    ):
        with pytest.raises(personas.PersonaError):
            personas.clean_components(bad)  # type: ignore[arg-type]
    with pytest.raises(personas.PersonaError, match="secret"):
        personas.clean_components({"free_text": "Das Passwort: Sommer2024xyz bleibt geheim"})


# --- the questionnaire ----------------------------------------------------------------------------


def test_the_questionnaire_is_a_file_with_three_scenes_of_three_answers_and_six_questions() -> None:
    q = personas.questionnaire()
    assert len(q["scenes"]) == 3 and all(len(s["answers"]) == 3 for s in q["scenes"])
    assert [x["id"] for x in q["questions"]] == [
        "address", "humor", "length", "transparency", "ask", "emojis"
    ]  # fmt: skip
    for scene in q["scenes"]:
        assert scene["question"]["de"] and scene["question"]["en"]
        assert all(a["text"]["de"] and a["text"]["en"] for a in scene["answers"])
    defaults = {x["id"]: x["default"] for x in q["questions"]}
    assert defaults == {
        "address": "du", "humor": "1", "length": "short", "transparency": "true",
        "ask": "ask_first", "emojis": "false",
    }  # fmt: skip


def test_the_questionnaire_builds_the_same_text_from_the_same_answers() -> None:
    answers = {"scenes": {"plex": "b", "network": "b", "everything": "a"}, "humor": "2"}
    first = personas.answers_to_persona(answers)
    second = personas.answers_to_persona(dict(answers))
    assert first == second
    comp, example, role = first
    assert role == "friend" and comp["humor"] == 2 and comp["address"] == "du"
    assert personas.render(comp, example, "de").text == personas.render(comp, example, "de").text
    assert personas.render(comp, example, "de").tokens <= personas.MAX_TOKENS


def test_the_role_is_the_most_voted_ties_in_the_order_of_the_roles_none_is_neutral() -> None:
    assert personas.role_of({"plex": "a", "network": "b", "everything": "c"}) == "butler"
    assert personas.role_of({"plex": "c", "network": "c", "everything": "a"}) == "copilot"
    assert personas.role_of({"plex": "none", "network": "none", "everything": "none"}) == "neutral"
    assert personas.role_of({}) == "neutral"
    assert personas.role_of({"plex": "none", "network": "b"}) == "friend"


def test_unknown_answers_are_refused_and_the_preview_has_two_sample_sentences() -> None:
    for bad in (
        {"scenes": {"plex": "z"}},
        {"scenes": {"nope": "a"}},
        {"humor": "9"},
        {"address": "ihr"},
    ):
        with pytest.raises(personas.PersonaError):
            personas.answers_to_persona(bad)
    for lang in ("de", "en"):
        assert len(personas.samples_for({"scenes": {"plex": "a"}}, lang)) == 2
    off = personas.samples_for({"scenes": {"plex": "a"}, "humor": "0"}, "de")
    assert off == personas.questionnaire()["samples"]["neutral"]["de"]  # no humor, no quips


# --- the infrastructure profiles ------------------------------------------------------------------

PROFILE = """# node-amd

active · 2026-09-20 · amd-box, gpu-host · server

- Runs the llama-server for the small models.
prüfen: systemctl status llama

## Details
More text that is not mirrored.
"""


def test_a_steckbrief_is_parsed_into_a_note_with_status_date_aliases_and_kind() -> None:
    p = core_mirror.parse("steckbriefe/node-amd.md", PROFILE)
    assert p is not None
    assert (p.title, p.status, p.kind) == ("node-amd", "active", "server")
    assert p.aliases == ["amd-box", "gpu-host"]
    assert p.verified == datetime(2026, 9, 20, tzinfo=UTC)
    assert p.check_hint == "systemctl status llama"
    text = p.note_text()
    assert "node-amd" in text and "amd-box" in text and "llama-server" in text
    assert "More text" not in text and len(text) <= core_mirror.NOTE_MAX
    labelled = "# svc\n\nstatus: ok · verified: 03.09.2026 · alias: a · kind: docker\n\nText.\n"
    q = core_mirror.parse("steckbriefe/svc.md", labelled)
    assert q is not None and q.status == "ok" and q.kind == "docker"
    assert q.verified == datetime(2026, 9, 3, tzinfo=UTC)
    assert core_mirror.parse("steckbriefe/none.md", "# only a title\n\ntext\n") is None
    assert core_mirror.parse("steckbriefe/none.md", "no heading\n\na · b · c\n") is None


# --- the review of the core -----------------------------------------------------------------------


def test_the_review_proposes_unpinning_a_fact_that_is_given_often_and_never_used() -> None:
    idle = item("Pinned, never used", core=True, recalled_count=9, used_count=0)
    busy = item("Pinned and used", core=True, recalled_count=9, used_count=4)
    rule = item("A rule", "rule", core=True, recalled_count=50, used_count=0)
    found = core_review.unpin_candidates([idle, busy, rule])
    assert [f.item for f in found] == [idle] and found[0].action == "unpin"


def test_the_review_proposes_pinning_a_fact_that_is_used_and_reliable() -> None:
    used = item("Likes tea", "preference", used_count=8, recalled_count=9,
                confirmed_count=3, last_used_at=NOW - timedelta(days=1))  # fmt: skip
    rare = item("Once mentioned", "preference", used_count=1, recalled_count=1)
    volatile = item("Is ill today", "person", used_count=9, expires_at=NOW + timedelta(hours=3))
    found = core_review.pin_candidates([used, rare, volatile], NOW, memory_care.DEFAULT)
    assert [f.item for f in found] == [used] and found[0].action == "pin"


def test_a_contradiction_verdict_needs_valid_numbers_and_a_side_to_drop() -> None:
    a, b, c = item("Address me as du", "style"), item("Address me as Sie", "style"), item("Other")
    entries = [a, b, c]
    good = core_review.parse_conflicts(
        {"conflicts": [{"a": 1, "b": 2, "drop": "a", "reason": "du or Sie"}]}, entries
    )
    assert [(f.action, f.item) for f in good] == [("remove", a)] and good[0].other is b
    reword = core_review.parse_conflicts(
        {"conflicts": [{"a": 1, "b": 2, "drop": "b", "reword": "Use du.", "reason": "x"}]}, entries
    )
    assert reword[0].action == "change" and reword[0].text == "Use du." and reword[0].item is b
    for bad in (
        {"conflicts": [{"a": 1, "b": 9, "drop": "a"}]},
        {"conflicts": [{"a": 1, "b": 1, "drop": "a"}]},
        {"conflicts": [{"a": 1, "b": 2}]},
        {"conflicts": "none"},
        None,
    ):
        assert core_review.parse_conflicts(bad, entries) == []  # type: ignore[arg-type]
    assert core_review.fingerprint(entries) == core_review.fingerprint(list(entries))
    c.version = 2
    assert core_review.fingerprint(entries) != core_review.fingerprint([a, b, item("x")])


ONE_LINE = (
    "# nextcloud — Nextcloud (Dienst, Host-a)\n\n"
    "status: current · verified: 2026-10-01 [live] · aliases: drive · kind: service · "
    "host: alpha · hängt ab von: keycloak Nextcloud 34.0.4 läuft. "
    "prüfen: `occ status`, `systemctl is-active nextcloud-cron.timer`\n"
)


def test_a_check_hint_at_the_end_of_a_one_line_profile_and_the_host_are_read() -> None:
    p = core_mirror.parse("Steckbriefe/nextcloud.md", ONE_LINE)
    assert p is not None
    assert p.check_hint == "`occ status`, `systemctl is-active nextcloud-cron.timer`"
    assert p.host == "alpha"
    assert p.kind == "service" and p.status == "current"


def test_a_host_profile_is_its_own_host() -> None:
    text = "# delta — Proxmox-Host (Host)\n\nstatus: current · verified: 2026-10-01 · kind: host\n"
    p = core_mirror.parse("Steckbriefe/delta.md", text)
    assert p is not None and p.host == "delta"
