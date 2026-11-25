"""vet_lines: only the lines with a secret leave a chunk, the rest stays."""

from topcoat_hub import knowledge_rules as rules


def test_a_clean_chunk_is_unchanged() -> None:
    assert rules.vet_lines("Router reboots on sunday.\nIP is 10.0.0.1") == (
        "Router reboots on sunday.\nIP is 10.0.0.1",
        [],
    )


def test_only_the_line_with_the_secret_goes() -> None:
    text = "Printer in the cellar.\nPasswort: hunter2x\nToner changes in spring."
    body, removed = rules.vet_lines(text)
    assert body == "Printer in the cellar.\nToner changes in spring."
    assert removed == ["password"]


def test_a_secret_over_two_lines_takes_both_lines() -> None:
    body, removed = rules.vet_lines("Switch in the rack.\nPasswort:\nhunter2x\nFirmware 2024.")
    assert body is not None and "hunter2x" not in body and "Switch in the rack." in body
    assert removed


def test_a_chunk_that_is_only_a_secret_is_held_back() -> None:
    assert rules.vet_lines("Passwort: hunter2x") == (None, ["password"])


def test_a_private_key_holds_the_whole_chunk_back() -> None:
    body, removed = rules.vet_lines(
        "Backup key:\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    )
    assert body is None and removed == ["private_key"]


def test_several_secret_lines_are_all_named() -> None:
    body, removed = rules.vet_lines(
        "NAS in the hall.\nPasswort: hunter2x\nAPI key sk-abcdefghijklmnopqrstuvwxyz\nDone."
    )
    assert body == "NAS in the hall.\nDone."
    assert removed == ["api_key", "password"]
