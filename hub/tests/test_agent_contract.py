"""The employee contracts (agent_contract.py, plan step 1.9): they hold their schema, the prompts
of Development and Projekte carry no home-lab rules, the self-description speaks in words, and the
seeded tool ceilings are what each contract says."""

import json
import re
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

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
    """(b) of the acceptance: no function names, and the short one fits its room."""
    for key in agent_contract.KEYS:
        short = agent_contract.short(key)
        assert short.startswith("Ich bin ") and len(short) <= agent_contract.SHORT_CHARS, key
        assert "Ich kann " in short
        for text in (short, agent_contract.long(key)):
            assert not _TECHNICAL.search(text), (key, _TECHNICAL.search(text))
    assert agent_contract.short("files") == "" and agent_contract.prompt_block("files") == ""
    view = agent_contract.view("dev")
    assert view is not None and view["title"] == "Entwicklung" and view["needs_click"]


def test_the_seeded_tool_ceilings_are_the_contracts() -> None:
    """The ceiling an admin starts from (ui/seeds.py) is the contract's list of tools: no group and
    no internet beyond what the contract explains, so the self-description covers every tool the
    program may use, and nothing the contract offers is missing. Programs without a contract (Files,
    Wiki, ...) have no tools."""
    for program in seeds.builtin_templates():
        contract = agent_contract.get(program["key"])
        scope = program["agent"]["tool_scope"]
        if contract is None:
            assert scope["groups"] == [] and scope["internet"] is False, program["key"]
            continue
        assert set(scope["groups"]) <= set(contract.groups), program["key"]
        assert set(scope["groups"]) == set(contract.groups), program["key"]
        assert scope["internet"] is contract.internet, program["key"]
        assert scope["nodes"] is None  # a contract names no machines
    admin = next(t for t in seeds.builtin_templates() if t["key"] == "admin")
    assert admin["agent"]["tool_scope"]["internet"] is False  # it is not in admin.json


def _copy_contracts(tmp_path: Path) -> Path:
    root = tmp_path / "proto"
    shutil.copytree(agent_contract._PROTO[0] / "agents", root / "agents")
    return root


def _edit(root: Path, key: str, change: Callable[[dict[str, Any]], None]) -> None:
    path = root / "agents" / f"{key}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    change(raw)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def proto_copy(tmp_path: Path) -> Iterator[Path]:
    before = agent_contract._PROTO[0]
    root = _copy_contracts(tmp_path)
    agent_contract.set_proto_root(root)
    try:
        yield root
    finally:
        agent_contract.set_proto_root(before)


def test_changing_a_contract_changes_the_seed(proto_copy: Path) -> None:
    """One source: a tool dropped from or added to a contract is gone from or in the seed."""

    def drop_github_add_internet(raw: dict[str, Any]) -> None:
        raw["tools"] = [t for t in raw["tools"] if t["group"] != "github"]
        raw["tools"].append({"group": "internet", "say_de": "das Web", "say_en": "the web"})

    _edit(proto_copy, "dev", drop_github_add_internet)
    agent_contract.set_proto_root(proto_copy)  # the files are cached
    dev = next(t for t in seeds.builtin_templates() if t["key"] == "dev")
    assert "github" not in dev["agent"]["tool_scope"]["groups"]
    assert dev["agent"]["tool_scope"]["internet"] is True
    assert dev["agent"]["frame"] == seeds.FRAMES["dev"]  # what the contract cannot say stays


def test_a_broken_contract_is_an_error_not_a_program_without_tools(proto_copy: Path) -> None:
    (proto_copy / "agents" / "admin.json").write_text("{", encoding="utf-8")
    agent_contract.set_proto_root(proto_copy)
    with pytest.raises(ValueError):
        seeds.builtin_templates()


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
    hint = chat_tools.abilities_hint(agent_contract.long("dev"))
    assert "Entwicklung" in hint and "never name a function" in hint.lower()
