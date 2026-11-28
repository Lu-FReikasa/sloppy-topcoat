"""#222: the short answer under a report says only what the evidence holds."""

from topcoat_hub.unchecked_advice import mark

EVIDENCE = (
    "Result of the server check: Summary: the controller logs no client events.\n"
    '[live E1] {"page": "Settings > Site > Services", "label": "Rights & capabilities", '
    '"switch": "F\\u00e4higkeiten"}'
)


def test_a_menu_path_no_evidence_holds_is_marked_as_not_checked() -> None:
    answer = (
        "The controller logs no client events. To turn it on, open the Omada controller and go to "
        "Settings → Client event logging → Log Client Events."
    )
    out, found = mark(answer, EVIDENCE, "en")
    assert found == ["Settings → Client event logging → Log Client Events"]
    # the finding the evidence holds stays as it is; the guessed path is marked in place
    assert out.startswith("The controller logs no client events. *Not checked:* To turn it on")
    assert out.endswith("Log Client Events.")


def test_a_path_the_evidence_holds_stays_as_it_is_also_with_words_around_it() -> None:
    answer = "Open Settings > Site > Services and pick the service there."
    assert mark(answer, EVIDENCE, "en") == (answer, [])


def test_a_quoted_setting_without_evidence_is_marked_in_the_users_language() -> None:
    answer = "- Schalte in den Einstellungen die Option „Log Client Events“ ein.\n- Sonst nichts."
    out, found = mark(answer, EVIDENCE, "de")
    assert found == ["Log Client Events"]
    assert out == (
        "- *Nicht geprüft:* Schalte in den Einstellungen die Option „Log Client Events“ ein.\n"
        "- Sonst nichts."
    )


def test_a_label_in_json_escapes_counts_as_backed() -> None:
    answer = 'The tab "Rights & capabilities" has the switch "Fähigkeiten".'
    assert mark(answer, EVIDENCE, "en") == (answer, [])


def test_a_sentence_that_marks_itself_and_a_code_block_are_left_alone() -> None:
    answer = (
        "Not checked: the menu is probably Settings > Logging > Clients.\n"
        "[assumption] It may be under System > Logs.\n"
        "```bash\n"
        "echo Settings > Out.txt\n"
        "```"
    )
    assert mark(answer, EVIDENCE, "en") == (answer, [])


def test_comparisons_and_plain_quotes_are_no_menu_paths() -> None:
    answer = 'The load is high when CPU > 90 %. He called it "a slow week".'
    assert mark(answer, EVIDENCE, "en") == (answer, [])


def test_a_chain_of_causes_with_arrows_is_no_menu_path() -> None:
    """Review of #222: arrows in a chain of causes or a data flow lead nowhere in a GUI (and in
    German every noun starts with a capital, so a chain looks like labels)."""
    for answer, language in [
        ("OOM → Restart → Service back.", "en"),
        ("The flow is Client → Proxy → Web.", "en"),
        ("Use GPU > CPU for inference.", "en"),
        ("Speicher voll → Container neu gestartet → Dienst wieder da.", "de"),
        ("Ursache: Zu wenig RAM → OOM-Kill.", "de"),
    ]:
        assert mark(answer, EVIDENCE, language) == (answer, [])
    # a sentence that leads somewhere still counts, in either language
    assert mark("Öffne Einstellungen → Protokoll → Clients.", EVIDENCE, "de")[1]
    assert mark("Click Logging → Clients.", EVIDENCE, "en")[1]


def test_every_unbacked_sentence_is_marked_also_past_the_logged_ones() -> None:
    answer = "\n".join(f"- Go to Settings → Area {n} → Item {n}." for n in range(8))
    out, found = mark(answer, EVIDENCE, "en")
    assert len(found) == 5  # what the log names
    assert out.count("*Not checked:*") == 8


def test_a_long_run_of_label_characters_takes_no_time() -> None:
    """Review of #222: unbounded label words made "A/A/A/..." quadratic (20 s for 40 000 chars)."""
    import time

    start = time.monotonic()
    for run in ("A/" * 20000, "A(" * 20000, "A'" * 20000):
        mark("Open the menu " + run, EVIDENCE, "en")
    assert time.monotonic() - start < 2
