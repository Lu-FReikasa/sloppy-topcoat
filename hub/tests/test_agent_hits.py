"""A read that names a hit of the run's own search in its own words reads that hit (agent_hits.py).
The cases are the failed calls of the live runs (2026-11-04..11)."""

from topcoat_hub.agent_hits import Hits

NOTES = [
    "akte-20261107-bitte-fuehre-einen-abgleich-zwischen-den-786582c2.md",
    "Hub Aufbau~e234d66d-16b2-4153-a9e8-2cef9e90218a.md",
    "Topcoat Überblick~47e8f828-a775-4f75-907d-f50267a94040.md",
]


def _hits() -> Hits:
    h = Hits()
    h.note("knowledge.search", {"hits": [{"path": p, "score": 1} for p in NOTES]}, "E2")
    h.note("knowledge.search", {"hits": [{"path": NOTES[1]}]}, "E3")
    return h


def test_a_hit_named_exactly_is_left_alone() -> None:
    assert _hits().resolve("knowledge.read", {"path": NOTES[0]}) is None


def test_a_folder_in_front_and_a_name_cut_at_the_tilde_read_the_one_hit() -> None:
    out = _hits().resolve(
        "knowledge.read", {"path": "Topcoat Doku/Topcoat Überblick~...", "offset": 0}
    )
    assert out is not None
    assert out[0] == {"path": NOTES[2], "offset": 0} and "a hit of your search" in out[1]


def test_the_mark_of_a_search_with_one_hit_reads_that_hit() -> None:
    h = _hits()
    assert h.resolve("knowledge.read", {"path": "E3"})[0]["path"] == NOTES[1]
    assert h.resolve("knowledge.read", {"path": "E2"}) is None  # three hits: not guessed


def test_a_hit_number() -> None:
    h = _hits()
    assert h.resolve("knowledge.read", {"hit": 3})[0] == {"path": NOTES[2]}
    assert h.resolve("knowledge.read", {"path": "hit 2"})[0]["path"] == NOTES[1]
    assert h.resolve("knowledge.read", {"hit": 9}) is None


def test_nothing_fits_or_several_fit_stays() -> None:
    h = _hits()
    assert h.resolve("knowledge.read", {"path": "docs/THEMES.md"}) is None
    assert h.resolve("repo.read", {"path": "Hub Aufbau"}) is None  # no repo search yet
    h.note(
        "repo.search",
        {"matches": [{"path": "web/src/a/theme.ts"}, {"path": "web/src/b/theme.ts"}]},
        "E5",
    )
    assert h.resolve("repo.read", {"path": "theme.ts"}) is None


def test_web_hits_feed_only_web_fetch_and_the_failure_lists_them() -> None:
    h = Hits()
    h.note(
        "web.search",
        {"results": [{"url": "https://example.org/Docs/A"}, {"url": "https://example.org/b"}]},
        "E1",
    )
    assert (
        h.resolve("web.fetch", {"url": "https://example.org/docs/a"})[0]["url"]
        == "https://example.org/Docs/A"
    )
    assert h.resolve("knowledge.read", {"path": "https://example.org/b"}) is None
    text = h.listing("web.fetch")
    assert "1: https://example.org/Docs/A" in text and '"hit": N' in text
    assert h.listing("knowledge.read") == ""


def test_a_shortened_id_is_the_one_full_id_the_run_has_read() -> None:
    """Live 2026-11-18: gpt-oss called project.get with "281df60f-ce8f-4944-b0..." in two of four
    Projekte runs, right after project.list had shown the whole id."""
    from topcoat_hub.agent_loop import expand_short_ids

    full = "281df60f-ce8f-4944-b00e-6875c378b528"
    other = "281df60f-aaaa-4944-b00e-6875c378b528"
    seen = f'{{"projects":[{{"id":"{full}"}},{{"id":"a665199d-3da9-443e-9bb4-1a3dee6541bd"}}]}}'
    value, notes = expand_short_ids({"project_id": "281df60f-ce8f-4944-b0...", "n": 3}, seen)
    assert value == {"project_id": full, "n": 3} and len(notes) == 1
    # nested, and the typographic ellipsis
    value, _ = expand_short_ids({"ops": [{"node": "281df60f-ce8f…"}]}, seen)
    assert value == {"ops": [{"node": full}]}
    # two ids fit, or none: it stays and the tool refuses it as before
    assert expand_short_ids({"id": "281df60f..."}, seen + other)[0] == {"id": "281df60f..."}
    assert expand_short_ids({"id": "ffffffff-ce8f..."}, seen)[0] == {"id": "ffffffff-ce8f..."}
    # a text that only ends in dots is no id
    assert expand_short_ids({"q": "Bilder ..."}, seen)[0] == {"q": "Bilder ..."}


def test_a_garbled_id_is_the_one_it_shares_its_beginning_with() -> None:
    """Live 2026-11-18: "22c53e4b-3c6a-4df5-a3ae-ba735b?" (two characters dropped inside)."""
    from topcoat_hub.agent_loop import expand_short_ids

    full = "22c53e4b-3c6a-4df5-a3ae-ba73589b6752"
    near = "22c53e4b-3c6a-4df5-aaaa-ba73589b6752"  # shares 19 characters only
    seen = f'[{{"id":"{full}"}},{{"id":"{near}"}}]'
    value, notes = expand_short_ids({"project_id": "22c53e4b-3c6a-4df5-a3ae-ba735b?"}, seen)
    assert value == {"project_id": full} and len(notes) == 1
    # a valid id stays, also one the run has not read
    other = "11111111-2222-3333-4444-555555555555"
    assert expand_short_ids({"id": other}, seen)[0] == {"id": other}
    # too little in common: it stays and the tool refuses it
    assert expand_short_ids({"id": "22c53e4b-ffff?"}, seen)[0] == {"id": "22c53e4b-ffff?"}
