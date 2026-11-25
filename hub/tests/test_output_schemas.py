"""The forms a model's answer must have (output_schemas.py, plan step 1.12): the schemas are
valid, a block that names one sends it as response_format, a server that refuses it is asked
without it once and remembered, and the check block uses its form."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from topcoat_hub import output_schemas
from topcoat_hub.blocks import builtin, service
from topcoat_hub.blocks.engine import BlockEngine
from topcoat_hub.blocks.program import ProgramError, validate
from topcoat_hub.gateway import GatewayError
from topcoat_hub.model_resolver import ModelResolver
from topcoat_hub.node_policy import NodePolicy

PLAN = {
    "summary": "Leere Texte zählen 0 Wörter",
    "steps": [{"path": "hub/src/topcoat_hub/x.py", "symbol": "Counter.count", "where": "in",
               "do": "return 0 for None", "after": []}],
    "test": {"path": "hub/tests/test_x_none.py", "do": "None counts 0"},
}  # fmt: skip


def test_every_schema_is_valid_and_takes_its_answers() -> None:
    for name in output_schemas.NAMES:
        schema = output_schemas.get(name)
        assert schema is not None, name
        Draft202012Validator.check_schema(schema)
    Draft202012Validator(output_schemas.get("plan_v2")).validate(PLAN)
    Draft202012Validator(output_schemas.get("verdict")).validate({"ok": False, "problems": ["x"]})
    Draft202012Validator(output_schemas.get("check")).validate({"ok": True, "issues": []})
    report = {"title": "t", "problem": "p", "checks": ["c"], "cause": "c", "fix": "f",
              **PLAN}  # fmt: skip
    investigate = Draft202012Validator(output_schemas.get("investigate"))
    investigate.validate({"report": report})
    investigate.validate({"questions": [{"to": "dev", "question": "q?"}]})
    assert not Draft202012Validator(output_schemas.get("plan_v2")).is_valid(
        {**PLAN, "steps": [{**PLAN["steps"][0], "where": "line 147"}]}
    )
    assert output_schemas.get("nothing") is None
    fmt = output_schemas.response_format("verdict")
    assert fmt is not None and fmt["type"] == "json_schema" and "$schema" not in json.dumps(fmt)


def test_the_plan_programs_name_their_forms_and_an_unknown_form_is_refused() -> None:
    def forms(program: dict[str, Any]) -> list[str]:
        return [b["schema"] for b in program["stack"] if isinstance(b, dict) and "schema" in b]

    assert forms(builtin.ISSUE_STEPS) == ["plan_v2"]
    assert forms(builtin.INVESTIGATE_ROUND) == ["investigate"]
    assert forms(builtin.REVIEW_PLAN) == ["verdict"]
    bad = {"name": "x", "title": "x", "inputs": {"question": "text"},
           "stack": [{"b": "ask", "prompt": "p", "out": "o", "schema": "free"}]}  # fmt: skip
    with pytest.raises(ProgramError, match="schema"):
        validate(bad)


class Ports:
    def __init__(self) -> None:
        self.schemas: list[Any] = []

    async def ask(self, messages, picked, max_tokens, temperature, think=None, **kw):  # type: ignore[no-untyped-def]
        self.schemas.append(kw.get("schema"))
        return '{"ok": true, "issues": []}', "m"

    async def candidates(self) -> list[Any]:
        from topcoat_hub.blocks.pick import Candidate

        return [Candidate("m", "node-a", loaded=True), Candidate("other", "node-b", loaded=True)]


@pytest.mark.asyncio
async def test_a_block_with_a_form_sends_it_and_the_check_block_uses_its_own() -> None:
    program = {
        "name": "x", "title": "x", "inputs": {"question": "text"},
        "stack": [
            {"b": "ask", "prompt": "plan", "out": "a", "schema": "plan_v2"},
            {"b": "ask", "prompt": "free text", "out": "b"},
            {"b": "check", "in": "b", "out": "v"},
        ],
    }  # fmt: skip
    ports = Ports()
    await BlockEngine(ports).run(program, {"question": "q"})  # type: ignore[arg-type]
    assert ports.schemas[0] == output_schemas.get("plan_v2")
    assert ports.schemas[1] is None  # a port without the keyword is called as before
    assert ports.schemas[2] == output_schemas.get("check")


OK = {"choices": [{"message": {"content": '{"ok": true}'}}]}


class Gateway:
    def __init__(self, answers: list[object]) -> None:
        self.policy = NodePolicy(house={"node-tower": "coder"})
        self.resolver = ModelResolver(self)  # type: ignore[arg-type]
        self.answers = answers
        self.payloads: list[dict[str, Any]] = []

    async def route(self, requested: str) -> SimpleNamespace:
        return SimpleNamespace(candidates=[requested.partition("@")[2]])

    def _has_room(self, node: str) -> bool:
        return True

    async def call_streamed(self, requested, path, payload, label):  # type: ignore[no-untyped-def]
        self.payloads.append(dict(payload))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.mark.asyncio
async def test_a_server_that_refuses_the_form_is_asked_without_it_and_remembered() -> None:
    service._NO_SCHEMA.discard("coder")
    gw = Gateway([GatewayError(400, "bad_request", "response_format not supported"), OK, OK])
    ports = service.HubBlockPorts(gw, None, None, None, SimpleNamespace())  # type: ignore[arg-type]
    schema = output_schemas.get("verdict")
    msgs = [{"role": "user", "content": "x"}]
    assert (await ports.ask(msgs, None, 100, 0.0, False, schema=schema))[0] == '{"ok": true}'
    assert gw.payloads[0]["response_format"]["json_schema"]["schema"] == schema
    assert "response_format" not in gw.payloads[1]
    await ports.ask(msgs, None, 100, 0.0, False, schema=schema)  # remembered: not sent again
    assert "response_format" not in gw.payloads[2] and "coder" in service._NO_SCHEMA
    service._NO_SCHEMA.discard("coder")
