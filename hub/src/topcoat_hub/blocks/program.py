"""The block catalogue and the validator (PRG-00 R3).

A program is JSON::

    {"name": "report", "inputs": {"question": "text"}, "stack": [ {"b": "ask", ...}, ... ]}

Every block has a ``b`` (its kind) and the fields of its kind. Variables are typed and written
before they are read: ``text``, ``texts`` (a list), ``verdict`` (``ok``, ``issues``), ``compare``
(``agree``, ``verdict``, ``text``, ``differences``). The validator runs when a program is saved
and again before every run; what it lets through the engine can run:

* every variable is defined before it is read, with the type the block needs;
* every loop is bounded by a **progress guard** (it ends when a round changes nothing); a count is
  optional (``max``, 0 or absent = no count);
* the lanes of a ``parallel`` write different variables;
* a pick names only what a pick may (``pick.py``); a template only variables that exist.

A block never adds a right: tools, rights of the person and the node whitelist stand outside.
"""

import copy
import re
from typing import Any

from topcoat_hub import agent_hooks, templating
from topcoat_hub.applications import keys as application_keys
from topcoat_hub.bench.suites import SUITES
from topcoat_hub.blocks import pick as picks
from topcoat_hub.research_profile import DEPTHS

TYPES = ("text", "texts", "verdict", "compare", "bool")
MAX_DEPTH = 8
MAX_BLOCKS = 200
COMPARE_MODES = ("pick", "merge", "flag")
RULES = ("all", "majority")
# checks without a model that a check block adds to its model's verdict (engine._lint):
# theme = the hub's format check and css lint of a theme (ui/themes.lint_theme_answer)
LINTS = ("theme",)

# kind -> (required fields, optional fields); the checks of the values are in _check_block
CATALOGUE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "ask": (
        ("prompt", "out"),
        ("system", "pick", "max_tokens", "temperature", "think", "as", "role", "on_fail", "schema"),
    ),
    "agent": (("prompt", "out"), ("app", "depth", "pick", "hooks", "on_fail", "role")),
    "check": (("in", "out"), ("question", "pick", "think", "lint", "decides", "max_tokens")),
    "revise": (("draft", "out"), ("issues", "instruction", "pick", "think", "max_tokens")),
    "set": (("out", "value"), ()),
    "if": (("cond", "then"), ("else",)),
    "repeat_until": (("cond", "stack"), ("guard", "max")),
    "parallel": (("lanes",), ("max_parallel",)),
    "compare": (("in", "out"), ("mode", "rule", "pick", "think")),
    "for_each": (("in", "as", "stack"), ("collect", "into", "append", "max_parallel")),
    "stop": ((), ("reason",)),
    "file_report": (("in",), ("title",)),
    "tool": (("capability", "out"), ("input", "node", "app", "on_fail", "lines", "field", "ref")),
    "choose": (("in", "field", "cases", "out"), ("found",)),
    "ranges": (("in", "size", "out"), ("at",)),
}
# blocks a caller may give a model by its role (project_runs: "Modelle: judge=model@node")
ROLE_BLOCKS = ("ask", "agent")
ASK_AS = ("text", "texts")  # what an ask block writes: its answer, or the JSON list it answers
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class ProgramError(ValueError):
    """The program is not valid; the message says where."""


def _name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.isidentifier() or len(value) > 40:
        raise ProgramError(f"{where}: {value!r} is not a variable name")
    return value


def _stack(value: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(b, dict) for b in value):
        raise ProgramError(f"{where}: a stack is a list of blocks")
    return value


class _Walker:
    def __init__(self) -> None:
        self.count = 0

    def stack(self, blocks: Any, env: dict[str, str], path: str, depth: int) -> dict[str, str]:
        if depth > MAX_DEPTH:
            raise ProgramError(f"{path}: blocks are nested too deeply (at most {MAX_DEPTH})")
        env = dict(env)
        out: list[dict[str, Any]] = []
        for i, raw in enumerate(_stack(blocks, path)):
            env = self.block(raw, env, f"{path}[{i}]", depth)
            out.append(raw)
        blocks[:] = out
        return env

    # --- helpers ---------------------------------------------------------------------------

    def want(self, env: dict[str, str], name: Any, types: tuple[str, ...], where: str) -> str:
        name = _name(name, where)
        if name not in env:
            raise ProgramError(f"{where}: {name} is not defined before this block")
        if env[name] not in types:
            raise ProgramError(
                f"{where}: {name} is {env[name]}, this block needs {'/'.join(types)}"
            )
        return env[name]

    def template(self, text: Any, env: dict[str, str], where: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ProgramError(f"{where}: text is required")
        for ref in templating.references(text):
            if ref not in env:
                raise ProgramError(f"{where}: {{{{{ref}}}}} is not defined before this block")

    def think(self, block: dict[str, Any], where: str) -> None:
        """``think``: true or false (the model's reasoning); absent = the hub's default."""
        if "think" in block and not isinstance(block["think"], bool):
            raise ProgramError(f"{where}.think: true or false")

    def pick(self, block: dict[str, Any], env: dict[str, str], where: str) -> None:
        try:
            spec = picks.check_spec(block.get("pick"), f"{where}.pick")
        except picks.PickError as exc:
            raise ProgramError(str(exc)) from exc
        for key in ("same_as",):
            if key in spec:
                self.want(env, spec[key], ("text", "verdict", "compare"), f"{where}.pick.{key}")
        for var in spec.get("other_than", []):
            self.want(env, var, ("text", "verdict", "compare"), f"{where}.pick.other_than")
        if spec:
            block["pick"] = spec

    def cond(self, cond: Any, env: dict[str, str], where: str) -> None:
        if not isinstance(cond, str) or not cond.strip():
            raise ProgramError(f"{where}: cond is required (for example verdict.ok)")
        text = cond.strip()
        if text.startswith("not "):
            text = text[4:].strip()
        var, _, field = text.partition(".")
        # a list is true when it has items ("not files": the task named none)
        kind = self.want(env, var, ("bool", "verdict", "compare", "texts"), where)
        wanted = {"bool": "", "texts": "", "verdict": "ok", "compare": "agree"}[kind]
        if field != wanted:
            raise ProgramError(f"{where}: use {var}{'.' + wanted if wanted else ''} here")

    # --- the blocks ------------------------------------------------------------------------

    def block(
        self, b: dict[str, Any], env: dict[str, str], where: str, depth: int
    ) -> dict[str, str]:
        self.count += 1
        if self.count > MAX_BLOCKS:
            raise ProgramError(f"{where}: a program has at most {MAX_BLOCKS} blocks")
        kind = b.get("b")
        if kind not in CATALOGUE:
            raise ProgramError(f"{where}: unknown block {kind!r}")
        required, optional = CATALOGUE[kind]
        allowed = {"b", *required, *optional}
        if set(b) - allowed:
            raise ProgramError(f"{where}: {kind} has no field {sorted(set(b) - allowed)[0]!r}")
        for field in required:
            if field not in b:
                raise ProgramError(f"{where}: {kind} needs {field!r}")
        env = dict(env)
        self.think(b, where)
        if "schema" in b:
            from topcoat_hub.output_schemas import NAMES

            if b["schema"] not in NAMES:  # the forms in proto/agents/outputs (1.12)
                raise ProgramError(f"{where}.schema: one of {', '.join(NAMES)}")
        if "role" in b:
            _name(b["role"], f"{where}.role")  # a short name such as judge or author
        if kind == "choose":
            # a text that holds a JSON object names, in ``field``, which variable comes next:
            # cases maps each value to a variable; ``found`` says whether a case matched
            self.want(env, b["in"], ("text",), f"{where}.in")
            if not isinstance(b["field"], str) or not b["field"].strip():
                raise ProgramError(f"{where}.field: the name of a field of the object")
            cases = b["cases"]
            if not isinstance(cases, dict) or not cases or len(cases) > 40:
                raise ProgramError(f"{where}.cases: an object of 1 to 40 value -> variable")
            for value, var in cases.items():
                self.want(env, var, ("text",), f"{where}.cases.{value}")
            env[_name(b["out"], where)] = "text"
            if "found" in b:
                env[_name(b["found"], f"{where}.found")] = "bool"
        elif kind == "ask":
            self.template(b["prompt"], env, f"{where}.prompt")
            if "system" in b:
                self.template(b["system"], env, f"{where}.system")
            self.pick(b, env, where)
            for key, low, high in (("max_tokens", 1, 200_000), ("temperature", 0, 2)):
                v = b.get(key)
                if v is not None and (
                    isinstance(v, bool) or not isinstance(v, int | float) or not low <= v <= high
                ):
                    raise ProgramError(f"{where}.{key}: a number from {low} to {high}")
            if b.get("as", "text") not in ASK_AS:
                raise ProgramError(f"{where}.as: one of {', '.join(ASK_AS)}")
            if b.get("on_fail", "stop") not in ("stop", "empty"):
                raise ProgramError(f"{where}.on_fail: stop (the run ends) or empty (it goes on)")
            env[_name(b["out"], where)] = b.get("as", "text")
        elif kind == "tool":
            # one capability, called like the agent calls it: the person's grants, the node's
            # whitelist and the ceiling of the application ``app`` decide; the block adds nothing
            cap = b["capability"]
            if not isinstance(cap, str) or not _CAPABILITY.match(cap) or len(cap) > 60:
                raise ProgramError(f"{where}.capability: a capability name such as repo.list")
            if b.get("app", "research") not in application_keys():
                raise ProgramError(f"{where}.app: unknown application {b.get('app')!r}")
            if "node" in b and (not isinstance(b["node"], str) or not b["node"].strip()):
                raise ProgramError(f"{where}.node: a node name")
            given = b.get("input", {})
            if not isinstance(given, dict) or len(given) > 20:
                raise ProgramError(f"{where}.input: an object of at most 20 fields")
            for key, value in given.items():
                if isinstance(value, str):
                    if value.strip():
                        self.template(value, env, f"{where}.input.{key}")
                elif not isinstance(value, int | float | bool) or value is None:
                    raise ProgramError(f"{where}.input.{key}: text, a number or true/false")
            if b.get("on_fail", "stop") not in ("stop", "empty"):
                raise ProgramError(f"{where}.on_fail: stop (the run ends) or empty (it goes on)")
            if "lines" in b:  # "120-269": start_line and end_line of the call
                if not isinstance(b["lines"], str) or not b["lines"].strip():
                    raise ProgramError(f"{where}.lines: a text such as {{{{lines}}}} or 1-150")
                self.template(b["lines"], env, f"{where}.lines")
            if "field" in b and (not isinstance(b["field"], str) or not b["field"].strip()):
                raise ProgramError(
                    f"{where}.field: the name of a field of the answer, such as text"
                )
            env[_name(b["out"], where)] = "text"
            if "ref" in b:  # the number of the read (F3), for a block that cites it
                env[_name(b["ref"], f"{where}.ref")] = "text"
        elif kind == "ranges":
            # the line ranges of a file read by a tool (its total_lines), no model: "1-350", ...
            self.want(env, b["in"], ("text",), f"{where}.in")
            if "at" in b:  # a search for definitions: the parts begin where they begin
                self.want(env, b["at"], ("text",), f"{where}.at")
            size = b["size"]
            if isinstance(size, bool) or not isinstance(size, int) or not 20 <= size <= 5000:
                raise ProgramError(f"{where}.size: a count of lines from 20 to 5000")
            env[_name(b["out"], where)] = "texts"
        elif kind == "agent":
            self.template(b["prompt"], env, f"{where}.prompt")
            if b.get("app", "research") not in application_keys():
                raise ProgramError(f"{where}.app: unknown application {b.get('app')!r}")
            if b.get("depth", "default") not in DEPTHS:
                raise ProgramError(f"{where}.depth: one of {', '.join(DEPTHS)}")
            self.pick(b, env, where)
            try:
                agent_hooks.check(b.get("hooks"), f"{where}.hooks")
            except agent_hooks.HookError as exc:
                raise ProgramError(str(exc)) from exc
            if b.get("on_fail", "stop") not in ("stop", "empty"):
                raise ProgramError(f"{where}.on_fail: stop (the run ends) or empty (it goes on)")
            env[_name(b["out"], where)] = "text"
        elif kind == "check":
            self.want(env, b["in"], ("text",), f"{where}.in")
            if "question" in b:
                self.want(env, b["question"], ("text",), f"{where}.question")
            if "lint" in b and b["lint"] not in LINTS:
                raise ProgramError(f"{where}.lint: one of {', '.join(LINTS)}")
            if b.get("decides", "model") not in ("model", "lint"):
                raise ProgramError(f"{where}.decides: model or lint")
            if b.get("decides") == "lint" and "lint" not in b:
                raise ProgramError(f"{where}.decides: lint needs a lint")
            limit = b.get("max_tokens")
            if limit is not None and (
                isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200_000
            ):
                raise ProgramError(f"{where}.max_tokens: a number from 1 to 200000")
            self.pick(b, env, where)
            env[_name(b["out"], where)] = "verdict"
        elif kind == "revise":
            self.want(env, b["draft"], ("text",), f"{where}.draft")
            if "issues" in b:
                self.want(env, b["issues"], ("verdict", "compare"), f"{where}.issues")
            if "instruction" in b:
                self.template(b["instruction"], env, f"{where}.instruction")
            self.pick(b, env, where)
            env[_name(b["out"], where)] = "text"
        elif kind == "set":
            self.template(b["value"], env, f"{where}.value")
            env[_name(b["out"], where)] = "text"
        elif kind == "if":
            self.cond(b["cond"], env, f"{where}.cond")
            a = self.stack(b["then"], env, f"{where}.then", depth + 1)
            c = self.stack(b.get("else", []), env, f"{where}.else", depth + 1)
            b.setdefault("else", [])
            # only what both branches define (with one type) exists afterwards
            env.update({k: v for k, v in a.items() if c.get(k) == v and env.get(k) != v})
        elif kind == "repeat_until":
            # while not cond: body. The condition is read before every round (it exists before)
            self.cond(b["cond"], env, f"{where}.cond")
            guard = b.get("guard", "progress")
            if guard != "progress":
                raise ProgramError(f"{where}.guard: a loop ends by its progress guard")
            b["guard"] = guard
            limit = b.get("max", 0)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ProgramError(f"{where}.max: a count from 0 (0 = no count)")
            b["max"] = limit
            after = self.stack(b["stack"], env, f"{where}.stack", depth + 1)
            env = after
            if not written(b["stack"]):
                raise ProgramError(f"{where}: the body of a loop must write a variable")
        elif kind == "parallel":
            lanes = b["lanes"]
            if not isinstance(lanes, list) or not 2 <= len(lanes) <= 8:
                raise ProgramError(f"{where}.lanes: two to eight lanes")
            limit = b.get("max_parallel", 1)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ProgramError(f"{where}.max_parallel: at least 1")
            b["max_parallel"] = limit
            seen: dict[str, int] = {}
            merged: dict[str, str] = {}
            for i, lane in enumerate(lanes):
                lane_env = self.stack(lane, env, f"{where}.lanes[{i}]", depth + 1)
                for var in written(lane):
                    if var in seen:
                        raise ProgramError(
                            f"{where}: lanes {seen[var]} and {i} both write {var}; "
                            "each lane writes its own"
                        )
                    seen[var] = i
                    merged[var] = lane_env[var]
            env.update(merged)
        elif kind == "compare":
            names = b["in"]
            if not isinstance(names, list) or not 2 <= len(names) <= 3:
                raise ProgramError(f"{where}.in: two or three variables")
            kinds = {self.want(env, n, ("text", "verdict"), f"{where}.in") for n in names}
            if len(kinds) != 1:
                raise ProgramError(f"{where}.in: compare texts with texts, verdicts with verdicts")
            if kinds == {"verdict"}:
                if b.get("rule", "all") not in RULES:
                    raise ProgramError(f"{where}.rule: one of {', '.join(RULES)}")
                b.setdefault("rule", "all")
                env[_name(b["out"], where)] = "verdict"  # a fixed rule, no model
            else:
                if b.get("mode", "pick") not in COMPARE_MODES:
                    raise ProgramError(f"{where}.mode: one of {', '.join(COMPARE_MODES)}")
                b.setdefault("mode", "pick")
                spec = dict(b.get("pick") or {})
                spec.setdefault("other_than", list(names))  # the judge is not an author
                b["pick"] = spec
                self.pick(b, env, where)
                env[_name(b["out"], where)] = "compare"
        elif kind == "for_each":
            self.want(env, b["in"], ("texts",), f"{where}.in")
            inner = dict(env)
            inner[_name(b["as"], where)] = "text"
            after = self.stack(b["stack"], inner, f"{where}.stack", depth + 1)
            # collect: a text the stack writes, gathered after every round into a new list
            if ("collect" in b) != ("into" in b):
                raise ProgramError(f"{where}: collect and into go together")
            if "collect" in b:
                if after.get(_name(b["collect"], f"{where}.collect")) != "text":
                    raise ProgramError(f"{where}.collect: a text the stack of the loop writes")
                if b["collect"] not in written(b["stack"]):
                    raise ProgramError(f"{where}.collect: a text the stack of the loop writes")
                env[_name(b["into"], f"{where}.into")] = "texts"
            if not isinstance(b.get("append", False), bool):
                raise ProgramError(f"{where}.append: true or false")
            limit = b.get("max_parallel", 1)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
                raise ProgramError(f"{where}.max_parallel: 1 to 8 rounds at a time")
            if b.get("append") and "into" not in b:
                raise ProgramError(f"{where}.append: needs collect and into")
        elif kind == "stop":
            if "reason" in b and not isinstance(b["reason"], str):
                raise ProgramError(f"{where}.reason: text")
        elif kind == "file_report":
            self.want(env, b["in"], ("text",), f"{where}.in")
            if "title" in b:
                self.template(b["title"], env, f"{where}.title")
        return env


def with_roles(program: dict[str, Any], models: dict[str, str]) -> dict[str, Any]:
    """A copy of ``program`` in which every block with a ``role`` named in ``models`` asks exactly
    that model (``model`` or ``model@node``), whatever pick it had. A role nobody names keeps the
    block's own pick. Used to compare models on the same program without changing it."""
    out = copy.deepcopy(program)

    def walk(blocks: list[dict[str, Any]]) -> None:
        for b in blocks:
            role = b.get("role")
            if b.get("b") in ROLE_BLOCKS and isinstance(role, str) and role in models:
                b["pick"] = {"fixed": models[role]}
            for key in ("stack", "then", "else"):
                if isinstance(b.get(key), list):
                    walk(b[key])
            for lane in b.get("lanes", []) if isinstance(b.get("lanes"), list) else []:
                walk(lane)

    walk(out.get("stack") or [])
    return out


def roles(program: dict[str, Any]) -> list[str]:
    """The roles a program's blocks name, in order of first use."""
    found: list[str] = []

    def walk(blocks: list[dict[str, Any]]) -> None:
        for b in blocks:
            if isinstance(b.get("role"), str) and b["role"] not in found:
                found.append(b["role"])
            for key in ("stack", "then", "else"):
                if isinstance(b.get(key), list):
                    walk(b[key])
            for lane in b.get("lanes", []) if isinstance(b.get("lanes"), list) else []:
                walk(lane)

    walk(program.get("stack") or [])
    return found


def written(stack: list[dict[str, Any]]) -> list[str]:
    """The variables a stack writes, in order of first writing (nested stacks included)."""
    out: list[str] = []

    def add(name: Any) -> None:
        if isinstance(name, str) and name not in out:
            out.append(name)

    def walk(blocks: list[dict[str, Any]]) -> None:
        for b in blocks:
            if "out" in b:
                add(b["out"])
            if b.get("b") == "tool" and "ref" in b:
                add(b["ref"])
            for key in ("stack", "then", "else"):
                if key == "stack" and b.get("b") == "for_each" and "into" in b:
                    add(b["into"])  # what the loop gathers exists after it
                if isinstance(b.get(key), list):
                    walk(b[key])
            for lane in b.get("lanes", []) if isinstance(b.get("lanes"), list) else []:
                walk(lane)

    walk(stack)
    return out


def validate(program: Any) -> dict[str, Any]:
    """A checked copy of the program (defaults filled in), or a :class:`ProgramError`."""
    if not isinstance(program, dict) or set(program) - {"name", "inputs", "stack", "title"}:
        raise ProgramError("a program has name, title, inputs and stack")
    prog = copy.deepcopy(program)
    name = prog.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ProgramError("a program needs a name")
    inputs = prog.get("inputs") or {}
    if not isinstance(inputs, dict) or not inputs:
        raise ProgramError('a program needs at least one input, for example {"question": "text"}')
    env: dict[str, str] = {}
    for key, kind in inputs.items():
        if kind not in TYPES:
            raise ProgramError(f"inputs.{key}: type is one of {', '.join(TYPES)}")
        env[_name(key, "inputs")] = kind
    prog["inputs"] = inputs
    if not isinstance(prog.get("stack"), list) or not prog["stack"]:
        raise ProgramError("a program needs at least one block")
    _Walker().stack(prog["stack"], env, "stack", 0)
    return prog


def catalogue() -> dict[str, Any]:
    """The blocks for the editor: kind -> required and optional fields."""
    return {
        "types": list(TYPES),
        "blocks": {
            k: {"required": list(r), "optional": list(o)} for k, (r, o) in CATALOGUE.items()
        },
        "compare_modes": list(COMPARE_MODES),
        "ranks": list(picks.RANKS),
        "suites": list(SUITES),
        "hooks": sorted(agent_hooks.HOOK_KEYS),
        # for the typed editor: the yes/no hooks, the numeric ones with their range, the places
        # an agent block may work for and the depths of research
        "hook_booleans": list(agent_hooks.BOOLEANS),
        "hook_numbers": {k: list(v) for k, v in agent_hooks.NUMBERS.items()},
        "applications": list(application_keys()),
        "depths": list(DEPTHS),
    }
