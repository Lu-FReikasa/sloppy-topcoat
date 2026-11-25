"""The employee contracts (agent_contract.py, plan step 1.9): they hold their schema, the prompts
of Development and Projekte carry no home-lab rules, the self-description speaks in words, and the
seeded tool ceilings stay inside what each contract says."""

import re

from topcoat_hub import agent_contract, agent_prompts, local_tools, messages
from topcoat_hub.ui import seeds

# what a function or capability name looks like: snake_case, or a dotted name such as wiki.read
# (a product such as llama.cpp in an example is a name the person knows, not one of ours)
_TECHNICAL = re.compile(r"\b[a-z]+_[a-z_]+\b|\b[a-z]+\.(?!cpp\b)[a-z_]+\b")
TOOLS = [
    {"node": "hub", "capability": "wiki.read", "input_schema": {}, "description": "a page"},
    {"node": "hub", "capability": "health.check", "input_schema": {}, "description": "health"},
]


def test_every_contract_holds_its_schema() -> None:
    contracts = agent_contract.all_contracts()
    assert sorted(contracts) == sorted(agent_contract.KEYS)
    assert agent_contract.get("chat") is contracts["assistant"]
    assert agent_contract.get("files") is None


def test_development_and_projekte_prompts_have_no_home_lab_rules() -> None:
    """(a) of the acceptance: the role and its rules come from the contract; the bullets about
    machines are gone, the shared protocol stays."""
    for app in ("dev", "project"):
        prompt = agent_prompts.system_prompt(TOOLS, application=app)
        contract = agent_contract.get(app)
        assert contract is not None and prompt.startswith(contract.intro or "?")
        for word in ("home-lab", "Look before you conclude", "files.list and files.read",
                     "For a question about Topcoat itself"):  # fmt: skip
            assert word not in prompt, (app, word)
        assert "Every reply is exactly ONE JSON object" in prompt  # the protocol stays
        assert contract.rules[0] in prompt
    admin = agent_prompts.system_prompt(TOOLS, application="admin")
    assert admin.startswith("You are the diagnosis agent of a home-lab operator")
    assert "project.changeset.propose" in agent_contract.get("project").rules[0]


def test_the_self_description_speaks_in_words() -> None:
    """(b) of the acceptance: no function names, and the short one fits its room, in both
    languages."""
    for key in agent_contract.KEYS:
        for language, i_am, i_can in (("de", "Ich bin ", "Ich kann "), ("en", "I am ", "I can ")):
            short = agent_contract.short(key, language)
            assert short.startswith(i_am) and len(short) <= agent_contract.SHORT_CHARS, key
            assert i_can in short
            for text in (short, agent_contract.long(key, language)):
                assert not _TECHNICAL.search(text), (key, language, _TECHNICAL.search(text))
    assert agent_contract.short("files") == "" and agent_contract.prompt_block("files") == ""
    view = agent_contract.view("dev", "de")
    assert view is not None and view["title"] == "Entwicklung" and view["needs_click"]


def test_every_text_for_the_person_comes_in_both_languages() -> None:
    """Issue #6: each German text has an English one next to it, list for list."""
    for key, c in agent_contract.all_contracts().items():
        raw = c.raw
        assert raw["title_en"] and raw["purpose"]["en"], key
        for task in raw["tasks"]:
            assert task["en"] and task["example_en"], key
        for name in ("never", "needs_click"):
            assert len(raw["limits"][f"{name}_en"]) == len(raw["limits"][f"{name}_de"]), key
        for talk in raw["talks_to"]:
            assert talk["for_en"], key


def test_the_contract_speaks_the_language_it_is_asked_in() -> None:
    """English by default and for a language the contracts do not speak; German unchanged."""
    en = agent_contract.long("dev")
    assert en == agent_contract.long("dev", "en") == agent_contract.long("dev", "fr")
    assert en.startswith("**Development**: ")
    for heading in ("Typical tasks:", "What I may use:", "What I never do:",
                    "What needs your click:", "Whom I ask:"):  # fmt: skip
        assert heading in en
    assert "push to main or merge" in en and "- Administration: " in en
    assert "(for example: “Fix for issue 2.16”)" in en
    for german in ("Was ich nie tue", "Ich ", " und ", "„"):
        assert german not in en, german
    de = agent_contract.long("dev", "de")
    assert de.startswith("**Entwicklung**: ") and "Was ich nie tue:" in de
    assert "- Verwaltung: " in de and "(zum Beispiel: „Fix für Issue 2.16“)" in de
    assert "Typical tasks" not in de
    # the short one: the fixed sentences and the titles of the others in the same language
    short_en = agent_contract.short("assistant", "en")
    assert short_en.startswith("I am Assistant: ")
    assert "I cannot " in short_en and "For that I ask Research (web research)" in short_en
    assert " and " in short_en and "Recherche" not in short_en
    short_de = agent_contract.short("assistant", "de")
    assert "Dafür frage ich Recherche (Recherchen im Web)" in short_de
    # the GUI's view
    view = agent_contract.view("admin", "en")
    assert view is not None and view["language"] == "en" and view["title"] == "Administration"
    assert view["tasks"][0] == {
        "text": "Check the state of the machines and services, with evidence",
        "example": "Why is the hub slow?",
    }
    assert view["never"][0] == "open a shell or run arbitrary commands"
    assert view["talks_to"] == [{"to": "Projects", "for": "findings on a task"}]
    german = agent_contract.view("admin", "de")
    assert german is not None and german["talks_to"] == [
        {"to": "Projekte", "for": "Befunde zu einer Aufgabe"}
    ]
    assert agent_contract.get("dev").title() == "Development"
    assert agent_contract.get("dev").title("de") == "Entwicklung"


def test_the_seeded_tool_ceilings_lie_inside_the_contracts() -> None:
    """The ceiling an admin starts from (ui/seeds.py) names no group the contract does not
    explain, so the self-description covers every tool the program may use."""
    for program in seeds.BUILTIN_TEMPLATES:
        contract = agent_contract.get(program["key"])
        groups = (program["agent"].get("tool_scope") or {}).get("groups")
        if contract is None or not groups:
            continue
        assert set(groups) <= set(contract.groups), (
            program["key"],
            set(groups) - set(contract.groups),
        )


def test_the_registry_reads_the_responsibility_from_the_contract() -> None:
    assert messages.responsibility("dev", "de") == agent_contract.get("dev").purpose("de")
    assert messages.responsibility("nobody") == ""


def test_the_chat_offers_its_contract_as_a_function() -> None:
    names = [f["function"]["name"] for f in local_tools.definitions(set())]
    assert local_tools.ABOUT in names and local_tools.ABOUT in local_tools.LOCAL


def test_a_question_about_abilities_is_answered_from_the_contract() -> None:
    """Live 2026-11-25: asked "Auf welche Werkzeuge hast du Zugriff?", gpt-oss listed function
    names in a table; such a question now gets the contract and the rule to say it in words."""
    from topcoat_hub import chat_tools

    for text in ("Auf welche Werkzeuge hast du Zugriff?", "Was kannst du?", "what can you do",
                 "Welche Fähigkeiten hast du eigentlich?"):  # fmt: skip
        assert chat_tools.asks_about_abilities(text), text
    for text in ("Welche Container laufen?", "Prüf die Tests von Issue 2.18", ""):
        assert not chat_tools.asks_about_abilities(text), text
    hint = chat_tools.abilities_hint(agent_contract.long("dev", "de"))
    assert "Entwicklung" in hint and "never name a function" in hint.lower()


def test_what_can_you_do_is_answered_in_the_persons_language(client, connect) -> None:  # type: ignore[no-untyped-def]
    """Issue #6 through the chat: the self-description in front of the conversation is in the
    person's stored language (English without one), the contract for "what can you do?" in the
    language of the question."""
    import json

    from tests.conftest import AUTH
    from tests.fakenode import online_node
    from tests.test_chat import ask, new_conv, sent_messages, use_model

    node = online_node(client, connect, "node-a", loaded=["small"], context_length=32768)
    use_model(client)

    def asked(text: str) -> str:
        status, _events, r = ask(client, new_conv(client, application="dev"), text)
        assert status == 200, getattr(r, "text", r)
        return json.dumps(sent_messages(node), ensure_ascii=False)

    sent = asked("what can you do?")
    assert "I am Development: " in sent and "What I never do:" in sent
    assert "Ich bin Entwicklung" not in sent and "Was ich nie tue" not in sent
    sent = asked("Was kannst du?")  # a German question gets the German contract
    assert "Was ich nie tue:" in sent and "What I never do:" not in sent
    assert client.put("/api/me/language", headers=AUTH, json={"language": "de"}).is_success
    sent = asked("Was kannst du?")
    assert "Ich bin Entwicklung: " in sent and "Was ich nie tue:" in sent
    assert "I am Development" not in sent
