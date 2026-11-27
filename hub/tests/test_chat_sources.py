"""B-C: a back-reference that nothing in the conversation backs and a file path no result shows are
marked by the hub; nothing is judged by a model."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.test_chat import new_conv
from tests.test_chat_report import world  # noqa: F401 - the fixture: a tool node and a model node
from tests.test_chat_tools import ask_with
from topcoat_hub import chat


def test_a_back_reference_without_a_source_in_the_conversation_is_found() -> None:
    visible = "Wo läuft Plex? Plex läuft auf node-nvidia, Port 32400."
    assert chat.unsupported_backrefs("Beim letzten Mal war der Port 80 belegt.", visible) == [
        "Beim letzten Mal"
    ]
    assert (
        chat.unsupported_backrefs("Wie vorhin festgestellt läuft Plex auf node-nvidia.", visible)
        == []
    )
    assert chat.unsupported_backrefs("Alles in Ordnung, der Port ist frei.", visible) == []
    assert chat.unsupported_backrefs("As we discussed, the limit is too low.", "hello") == [
        "As we discussed"
    ]


def test_a_path_that_no_result_shows_is_marked_and_a_shown_one_is_not() -> None:
    sources = "[E1] shell.read\n$ cat web/src/looks.css\n.look { color: red }"
    found = chat.unsupported_paths(
        "Siehe web/css/looks.css und web/src/looks.css, auch /etc/hosts, https://example.org/a.css.",
        sources,
    )
    assert found == ["web/css/looks.css", "/etc/hosts"]
    assert chat.unsupported_paths("Ratio 3/4 und und/oder.", sources) == []


def test_a_slash_inside_a_word_is_no_path_but_one_at_a_word_start_is() -> None:
    """#225: "Verbindungs-/Trennungen" was read as the path `/Trennungen`; a model often writes
    the hyphen as U+2011, U+2010, a soft hyphen or a dash."""
    for hyphen in ("-", "\u2011", "\u2010", "\u00ad", "\u2013"):
        answer = f"Die Verbindungs{hyphen}/Trennungen am Ein{hyphen}/Ausgang, und/oder beides."
        assert chat.unsupported_paths(answer, "") == [], hyphen
    found = chat.unsupported_paths("Siehe /etc/hosts, `hub/src/x.py` und see /var/log/syslog.", "")
    assert found == ["/etc/hosts", "hub/src/x.py", "/var/log/syslog"]
    assert chat.unsupported_paths("(/etc/hosts) und **/etc/fstab** und „/etc/passwd“", "") == [
        "/etc/hosts",
        "/etc/fstab",
        "/etc/passwd",
    ]


def test_the_note_names_the_phrase_and_the_path_in_the_users_language() -> None:
    de = chat.source_notes("de", ["wie vorhin"], ["web/css/looks.css"])
    assert de.startswith("\n\nHinweis:") and "wie vorhin" in de and "`web/css/looks.css`" in de
    en = chat.source_notes("en", ["as before"], [])
    assert en.startswith("\n\nNote:") and "no source" in en
    assert chat.source_notes("de", [], []) == ""


def test_an_answer_with_an_invented_back_reference_carries_the_note(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    _, _, model = world
    model.stream_text_fn = lambda body: "Wie wir vorhin festgestellt haben, hängt es am Limit."
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "Wie geht es dir heute?", "off")
    assert status == 200 and events[-1][0] == "done"
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
    assert stored["content"].endswith("hat in diesem Verlauf keine Quelle.")
    assert "Hinweis:" in "".join(d["text"] for e, d in events if e == "delta")


def test_an_answer_that_only_says_what_the_chat_said_stays_as_it_is(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    _, _, model = world
    model.stream_text_fn = lambda body: "Plex läuft auf node-nvidia, wie vorhin gesagt."
    conv = new_conv(client)
    ask_with(client, conv, "Wo läuft Plex? Auf node-nvidia vermutlich.", "off")
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
    assert "Hinweis" not in stored["content"]
