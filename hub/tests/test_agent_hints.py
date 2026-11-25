"""What a diagnosis run is told before its first turn (live 2026-11-17, a Wi-Fi question about a
phone the person calls "Luxel"): a fixed order for Wi-Fi questions where the Omada tools are, and
the person's own notes about the names the question quotes."""

import json

from fastapi.testclient import TestClient

from tests.conftest import TEST_DB_URL
from tests.test_agent import finish, start, tool, use, vm, wait_for  # noqa: F401
from tests.test_memory_applications import seed
from tests.users import make_user
from topcoat_hub import agent_prompts as prompts

OMADA = [
    {"node": "tools", "capability": "omada.clients"},
    {"node": "tools", "capability": "omada.events"},
]
QUESTION = (
    "Ich verliere am Outdoor AP das WLAN. Kannst du in den Omada Logs von gestern etwas sehen? "
    'Mein Google Pixel 7 ("Luxel") sollte dort zu sehen sein.'
)


def test_the_wifi_order_comes_only_with_the_omada_tools_and_a_wifi_question() -> None:
    hint = prompts.wifi_hint(QUESTION, OMADA)
    assert "omada.clients with match=" in hint and "Pixel-7a is not a Pixel 7" in hint
    assert "no client event ran on an access point" in hint
    assert prompts.wifi_hint(QUESTION, [{"node": "vm", "capability": "docker.logs"}]) == ""
    assert prompts.wifi_hint("Wie voll ist die Platte von vm?", OMADA) == ""


def test_quoted_names_are_found_once_and_short() -> None:
    assert prompts.quoted_names(QUESTION) == ["Luxel"]
    assert prompts.quoted_names("\u201eLuxel\u201c und 'luxel' und \u00bbKenobi\u00ab") == [
        "Luxel",
        "Kenobi",
    ]
    long = '"' + "ein sehr langer Satz in Anführungszeichen mit vielen Wörtern" + '"'
    assert prompts.quoted_names(long) == []


def test_the_notes_about_a_quoted_name_reach_the_first_prompt(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    seed(
        "Der Nutzer verwendet ein Google Pixel 7 mit dem Namen 'Luxel'.",
        kind="person",
        application=None,
    )
    other, _ = make_user(TEST_DB_URL, "someone-else")
    seed("Luxel ist der Hund von jemand anderem.", kind="person", application=None, user=other)
    model = use(client, tool("vm-docker", "system.info"), finish("Nothing to see."))
    wait_for(
        client,
        start(client, 'Warum startet der Container "Luxel" auf vm neu?').json()["id"],
        "done",
    )
    first = str(model.calls[0][1]["content"])
    assert "the user's notes that mention names" in first
    assert "Google Pixel 7 mit dem Namen 'Luxel'" in first
    assert "Hund" not in first  # another person's note never reaches this run


def test_a_sources_own_note_on_its_reach_is_taken_once() -> None:
    note = "none of the 12 client events in this window happened on an access point"
    texts = {
        "E1": '{"items":[],"window":{"note":"' + note + '"}}',
        "E2": '{"items":[],"window":{"note":"' + note.replace("12", "30") + '"}}',
        "E3": '{"items":[],"note":"no entry contains Luxel"}',
        "E4": "plain log text",
    }
    assert prompts.source_notes(texts) == [f"{note} [E1]"]


def test_the_note_on_a_sources_reach_reaches_the_report(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    from tests.test_agent import task

    _, node = vm
    node.capability_answers["system.info"] = {
        "hostname": "vm",
        "window": {"note": "none of the 3 client events ran on an access point"},
    }
    use(client, tool("vm-docker", "system.info"), finish("Nothing in the log."))
    tid = start(client, "Warum startet web auf vm neu?").json()["id"]
    wait_for(client, tid, "done")
    notes = task(client, tid)["diagnosis"]["notes"]
    assert any("none of the 3 client events ran on an access point [E1]" in n for n in notes)


def test_parameters_that_leaked_into_the_summary_become_fields() -> None:
    # live 2026-11-17 (Qwen3-Coder): the findings stood in the summary, the field was empty
    from topcoat_hub.agent_prompts import Finish, parse_native

    leaked = (
        "Es gibt keine Bildübertragung im Chat.\nparameter=findings>\n"
        '[{"claim": "Keine Bildübertragung", "tag": "live", "evidence": ["E1"]}]\n</parameter>'
    )
    call = {
        "function": {
            "name": "finish",
            "arguments": json.dumps({"summary": leaked, "findings": []}),
        }
    }
    action = parse_native(call, {})
    assert isinstance(action, Finish)
    assert action.summary == "Es gibt keine Bildübertragung im Chat."
    assert action.findings and action.findings[0]["claim"] == "Keine Bildübertragung"
    # a summary without markers stays as it is
    plain = {"function": {"name": "finish", "arguments": json.dumps({"summary": "a=b> c"})}}
    assert parse_native(plain, {}).summary == "a=b> c"


def test_a_row_that_cites_everything_it_read_is_sent_back() -> None:
    # live 2026-11-17: "E1, ... E16" behind one row, refused by the second look three times
    from topcoat_hub.agent_prompts import Finish, blanket_evidence

    wide = Finish(
        summary="s",
        findings=[{"claim": "c", "evidence": ["E1", "E2"]}],
        next="",
        table=[{"Ergebnis": "keine Bilder", "Beleg": "E1, E2, E3, E4, E5, E6"}],
    )
    assert "row 1" in blanket_evidence(wide)
    narrow = Finish(
        summary="s",
        findings=[],
        next="",
        table=[{"Ergebnis": "Bild-Tool", "Beleg": "E7, Zeile 197"}],
    )
    assert blanket_evidence(narrow) == ""
