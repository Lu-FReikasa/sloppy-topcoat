"""The chat policy (docs/PLATFORM.md, 3.4 and 3.6): a setting states how the chat answers, by
effort; an unreadable policy changes nothing; only an admin sets it."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.users import make_user
from topcoat_hub import chat_policy, quality
from topcoat_hub.agent_loop import Limits

URL = "/api/scheduling/chat-policy"


def test_an_empty_policy_says_what_the_hub_did_before() -> None:
    for level in chat_policy.LEVELS:
        policy = chat_policy.from_doc({}, level)
        base = quality.check_limits(level, False)
        assert policy.apply_limits(base) == base  # the same numbers as quality.py
        assert policy.wants_second_look(level) is (level == "thorough")
        assert policy.recall and policy.write_memory and policy.node == ""
        assert policy.review is None and policy.reactions is None
    assert chat_policy.from_doc({}, "fast") == chat_policy.DEFAULT


def test_values_are_read_at_the_effort_of_the_answer() -> None:
    doc = chat_policy.check(
        {
            "recall": {"effort": {"fast": False, "balanced": True, "thorough": True}},
            "max_tool_calls": {"effort": {"fast": 1, "balanced": 5, "thorough": 9}},
            "node": "node-big",
        }
    )
    fast = chat_policy.from_doc(doc, "fast")
    careful = chat_policy.from_doc(doc, "thorough")
    assert fast.recall is False and careful.recall is True
    assert fast.max_tool_calls == 1 and careful.max_tool_calls == 9
    assert fast.node == "node-big"
    limits = fast.apply_limits(Limits())
    assert limits.max_tool_calls == 1 and limits.max_turns == Limits().max_turns  # not given


def test_a_level_a_table_leaves_out_takes_the_balanced_value() -> None:
    doc = chat_policy.check({"max_turns": {"effort": {"balanced": 6, "thorough": 20}}})
    assert chat_policy.from_doc(doc, "fast").max_turns == 6
    assert chat_policy.from_doc(doc, "thorough").max_turns == 20
    assert chat_policy.from_doc(doc, "nonsense").max_turns == 6


def test_review_and_reactions_follow_the_hub_unless_the_policy_says_otherwise() -> None:
    plain = chat_policy.from_doc({}, "fast")
    assert plain.wants_review(True) and not plain.wants_review(False)
    assert plain.wants_reactions(False) is False
    doc = chat_policy.check(
        {
            "review": {"effort": {"fast": False, "balanced": True, "thorough": True}},
            "reactions": False,
        }
    )
    fast, careful = chat_policy.from_doc(doc, "fast"), chat_policy.from_doc(doc, "thorough")
    assert fast.wants_review(True) is False and careful.wants_review(False) is True
    assert fast.wants_reactions(True) is False


@pytest.mark.parametrize(
    "bad",
    [
        {"colour": "red"},
        {"max_turns": 0},
        {"max_turns": 500},
        {"max_turns": True},
        {"time_budget": 5},
        {"recall": "yes"},
        {"node": "a@b"},
        {"max_turns": {"effort": {"extreme": 3}}},
        {"max_turns": {"effort": {}}},
        {"recall": {"effort": {"fast": 1}}},
        ["max_turns"],
    ],
)
def test_a_policy_that_does_not_make_sense_is_refused(bad: Any) -> None:
    with pytest.raises(chat_policy.PolicyError):
        chat_policy.check(bad)


def test_the_admin_reads_sets_and_resets_the_policy(client: TestClient) -> None:
    first = client.get(URL, headers=AUTH).json()
    assert first["policy"] == {} and "second_look" not in first["levels"]["thorough"]
    put = client.put(
        URL,
        headers=AUTH,
        json={
            "policy": {
                "second_look": {"effort": {"fast": False, "balanced": True, "thorough": True}},
                "max_turns": 9,
            }
        },
    )
    assert put.status_code == 200, put.text
    levels = put.json()["levels"]
    assert levels["fast"]["second_look"] is False and levels["balanced"]["second_look"] is True
    assert levels["fast"]["max_turns"] == 9
    assert client.get(URL, headers=AUTH).json()["policy"]["max_turns"] == 9
    bad = client.put(URL, headers=AUTH, json={"policy": {"max_turns": 0}})
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "invalid_chat_policy"
    reset = client.put(URL, headers=AUTH, json={"policy": {}})
    assert reset.json()["policy"] == {}
    assert client.get(URL, headers=AUTH).json()["policy"] == {}


def test_only_an_admin_sets_the_policy(client: TestClient, clean_db: str) -> None:
    _, user = make_user(clean_db, "plain-user")
    assert client.put(URL, headers=user, json={"policy": {}}).status_code == 403
    assert client.get(URL, headers=user).status_code == 403
    assert client.get(URL).status_code == 401


def test_the_stored_policy_reaches_the_answer(client: TestClient) -> None:
    client.put(URL, headers=AUTH, json={"policy": {"recall": False}})

    async def read() -> Any:
        return await chat_policy.load(client.app.state.sessionmaker, "balanced")

    assert client.portal.call(read).recall is False


class _Db:
    async def get(self, *_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the database is away")


class _Maker:
    def __call__(self) -> "_Maker":
        return self

    async def __aenter__(self) -> _Db:
        return _Db()

    async def __aexit__(self, *_a: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_a_policy_that_cannot_be_read_falls_back_to_the_hubs_own_rules() -> None:
    assert await chat_policy.load(_Maker(), "thorough") is chat_policy.DEFAULT  # type: ignore[arg-type]
