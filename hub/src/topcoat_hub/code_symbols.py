"""The named places of a source file (plan 2026-11-24, B4 "symbol instead of anchor", step 1.10).

A fix plan used to name its places by an *anchor*, a line copied verbatim from the file. That is
the hardest output for a 20-30B model: on 2026-11-23 eight of ten plans were refused, mostly for
anchors that were line numbers ("147-148"), stood three times, or were remembered wrong. A plan
now names a *symbol* (``Class.method``, a function, a component, a JSON key path, a Markdown
heading, a CSS selector) and the hub finds its lines here. When a name is not there, the hub
lists the nearest names (never lines).

Works on the text the hub read with ``repo.read`` (no node change): Python by ``ast``, TS/TSX/JS
by patterns on the declaration lines and brace counting, JSON by its key lines, Markdown by its
headings, CSS by its rule heads. Every file kind also has the symbol ``imports`` (the import lines
at its top, or the first line when there are none), where a step adds an import.

Pure functions, no I/O.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass

IMPORTS = "imports"
NEAREST = 5


@dataclass(frozen=True)
class Symbol:
    name: str  # "Class.method", "function", "a.b.c", "## Heading", ".selector"
    kind: str  # class, function, method, const, type, component, key, heading, rule, imports
    start: int  # first line, 0-based (a decorator or doc comment above belongs to it)
    end: int  # one past the last line

    @property
    def lines(self) -> int:
        return self.end - self.start


def _kind_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    for ext, kind in (
        ((".py",), "py"),
        ((".ts", ".tsx", ".js", ".jsx", ".mjs"), "ts"),
        ((".json",), "json"),
        ((".md",), "md"),
        ((".css",), "css"),
    ):
        if name.endswith(ext):
            return kind
    return ""


# --- Python ---------------------------------------------------------------------------------------


def _python(text: str) -> list[Symbol]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _python_lines(text)
    out: list[Symbol] = []

    def start_of(node: ast.AST) -> int:
        decos = getattr(node, "decorator_list", None) or []
        first = min([node.lineno, *(d.lineno for d in decos)])  # type: ignore[attr-defined]
        return first - 1

    def walk(body: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                name = prefix + node.name
                out.append(Symbol(name, "class", start_of(node), node.end_lineno or node.lineno))
                walk(node.body, name + ".", True)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                name = prefix + node.name
                kind = "method" if in_class else "function"
                out.append(Symbol(name, kind, start_of(node), node.end_lineno or node.lineno))
                walk(node.body, name + ".", False)
            elif isinstance(node, ast.Assign | ast.AnnAssign) and (not prefix or in_class):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        out.append(
                            Symbol(
                                prefix + t.id, "const", node.lineno - 1,
                                node.end_lineno or node.lineno,
                            )
                        )  # fmt: skip

    walk(tree.body, "", False)
    imports = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    if imports:
        out.append(Symbol(IMPORTS, IMPORTS, imports[0].lineno - 1, imports[-1].end_lineno or 0))
    return out


_PY_DEF = re.compile(r"^(\s*)(?:async\s+)?(def|class)\s+(\w+)")


def _python_lines(text: str) -> list[Symbol]:
    """A Python file that does not parse (a draft half done): ``def``/``class`` lines by their
    indentation."""
    lines = text.splitlines()
    out: list[Symbol] = []
    stack: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _PY_DEF.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        name = ".".join([*(n for _, n in stack), m.group(3)])
        end = next(
            (
                j
                for j in range(i + 1, len(lines))
                if lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) <= indent
            ),
            len(lines),
        )
        kind = "class" if m.group(2) == "class" else ("method" if stack else "function")
        out.append(Symbol(name, kind, i, end))
        stack.append((indent, m.group(3)))
    return out


# --- TS / TSX / JS --------------------------------------------------------------------------------

_TS_DECL = re.compile(
    r"^(\s*)(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(function\*?|class|interface|type|enum|const|let|var)\s+(\w+)"
)
# a method of a class or an object: "  async load() {", "  private x(a: T): R {"
_TS_METHOD = re.compile(
    r"^(\s+)(?:public\s+|private\s+|protected\s+|static\s+|readonly\s+|async\s+|get\s+|set\s+)*"
    r"(\w+)\s*(?:<[^>]*>)?\s*\([^;]*$"
)
_TS_IMPORT = re.compile(r"^\s*import\b")
_NOT_METHODS = {"if", "for", "while", "switch", "catch", "return", "function", "await", "new"}


def _ts_end(lines: list[str], start: int) -> int:
    """One past the last line of the declaration at ``start``: its braces closed, or, for one
    without a block (``const x = 1;``, ``type A = B``), its statement ended."""
    depth = 0
    opened = False
    quote = ""
    for i in range(start, len(lines)):
        line = lines[i]
        j = 0
        while j < len(line):
            c = line[j]
            if quote:
                if c == "\\":
                    j += 1
                elif c == quote:
                    quote = ""
            elif c in "\"'`":
                quote = c
            elif line.startswith("//", j):
                break
            elif c in "{([":
                depth += 1
                opened = opened or c == "{"
            elif c in "})]":
                depth -= 1
            j += 1
        if quote == "`":
            continue  # a template string goes on
        quote = ""
        if depth > 0:
            continue
        if opened:
            return i + 1
        tail = line.split("//", 1)[0].rstrip()
        if tail and not tail.endswith(_GOES_ON):
            return i + 1
    return len(lines)


# a line ending so continues its statement on the next line
_GOES_ON = ("=", "=>", "(", ",", "?", ":", "&&", "||", "|", "+", "-", "*", ".")


def _ts_doc_start(lines: list[str], start: int) -> int:
    """A ``/** … */`` comment or decorators right above a declaration belong to it."""
    i = start
    while i > 0:
        above = lines[i - 1].strip()
        if above.endswith("*/") or above.startswith(("*", "/**", "@")):
            i -= 1
            continue
        break
    return i


def _ts(text: str) -> list[Symbol]:
    lines = text.splitlines()
    out: list[Symbol] = []
    owners: list[tuple[int, int, str]] = []  # (start, end, name) of classes and components
    for i, line in enumerate(lines):
        m = _TS_DECL.match(line)
        if not m:
            continue
        indent, word, name = len(m.group(1)), m.group(2), m.group(3)
        end = _ts_end(lines, i)
        owner = next((n for s, e, n in reversed(owners) if s < i < e), "")
        full = f"{owner}.{name}" if owner and indent else name
        if word.startswith("function"):
            kind = "component" if name[:1].isupper() else "function"
        elif word in ("const", "let", "var"):
            arrow = "=>" in " ".join(lines[i : min(i + 3, end)])
            kind = "component" if arrow and name[:1].isupper() else "function" if arrow else "const"
        else:
            kind = word
        out.append(Symbol(full, kind, _ts_doc_start(lines, i), end))
        if kind in ("class", "component") or (kind == "function" and not indent):
            owners.append((i, end, full))
    # methods of classes (a component's inner functions are its "const"/"function" lines above)
    for s, e, owner in owners:
        if not any(x.name == owner and x.kind == "class" for x in out):
            continue
        for i in range(s + 1, e):
            m = _TS_METHOD.match(lines[i])
            if m and m.group(2) not in _NOT_METHODS and lines[i].rstrip().endswith(("{", "(")):
                name = f"{owner}.{m.group(2)}"
                if not any(x.name == name for x in out):
                    out.append(Symbol(name, "method", _ts_doc_start(lines, i), _ts_end(lines, i)))
    imports = [i for i, ln in enumerate(lines) if _TS_IMPORT.match(ln)]
    if imports:
        out.append(Symbol(IMPORTS, IMPORTS, imports[0], _ts_end(lines, imports[-1])))
    return out


# --- JSON, Markdown, CSS --------------------------------------------------------------------------

_JSON_KEY = re.compile(r'^(\s*)"((?:[^"\\]|\\.)*)"\s*:')


def _json(text: str) -> list[Symbol]:
    """Keys by their lines and indentation (the catalogues are written one key a line)."""
    lines = text.splitlines()
    out: list[Symbol] = []
    stack: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _JSON_KEY.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([*(k for _, k in stack), m.group(2)])
        end = i + 1
        if line.rstrip().endswith(("{", "[")):
            end = next(
                (
                    j + 1
                    for j in range(i + 1, len(lines))
                    if lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) <= indent
                ),
                len(lines),
            )
            stack.append((indent, m.group(2)))
        out.append(Symbol(path, "key", i, end))
    return out


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _markdown(text: str) -> list[Symbol]:
    lines = text.splitlines()
    heads = [
        (i, len(m.group(1)), m.group(2)) for i, ln in enumerate(lines) if (m := _HEADING.match(ln))
    ]
    out: list[Symbol] = []
    for n, (i, level, title) in enumerate(heads):
        end = next((j for j, lv, _ in heads[n + 1 :] if lv <= level), len(lines))
        out.append(Symbol(f"{'#' * level} {title}", "heading", i, end))
    return out


_CSS_RULE = re.compile(r"^([^\s{}/][^{}]*?)\s*\{")


def _css(text: str) -> list[Symbol]:
    lines = text.splitlines()
    out: list[Symbol] = []
    for i, line in enumerate(lines):
        m = _CSS_RULE.match(line)
        if m:
            out.append(Symbol(" ".join(m.group(1).split()), "rule", i, _ts_end(lines, i)))
    return out


# --- public ---------------------------------------------------------------------------------------


def symbols(path: str, text: str) -> list[Symbol]:
    """The named places of ``text`` (the file ``path``), in the order they stand."""
    kind = _kind_of(path)
    found = {
        "py": _python,
        "ts": _ts,
        "json": _json,
        "md": _markdown,
        "css": _css,
    }.get(kind, lambda _t: [])(text)
    if kind and not any(s.name == IMPORTS for s in found):
        found.append(Symbol(IMPORTS, IMPORTS, 0, min(1, len(text.splitlines()))))
    return sorted(found, key=lambda s: (s.start, -s.end))


def _clean(name: str) -> str:
    """A name as a model writes it: ``def foo()``, ``Foo.bar()``, ```foo```, ``"a"."b"``."""
    want = name.strip().strip("`'\" ")
    want = re.sub(r"^(?:async\s+)?(?:def|class|function|const|interface|type)\s+", "", want)
    want = re.sub(r"\(.*\)\s*:?$", "", want)
    return want.replace('"."', ".").replace("::", ".").strip()


def find(path: str, text: str, name: str) -> tuple[list[Symbol], list[str]]:
    """``(matches, nearest names)``: the symbols ``name`` means (exactly, else by its last part
    when that is one symbol: ``ask`` for ``BlockService.ask``), and when none, the
    :data:`NEAREST` closest names of the file."""
    every = symbols(path, text)
    want = _clean(name)
    if not want:
        return [], []
    exact = [s for s in every if s.name == want]
    if not exact and _kind_of(path) == "md":
        bare = want.lstrip("#").strip().lower()
        exact = [s for s in every if s.name.lstrip("#").strip().lower() == bare]
    if exact:
        return exact, []
    # a bare name means the one symbol of that name (``ask`` for ``HubBlockPorts.ask``), or the
    # several the model must choose from; a dotted name is meant exactly (live 1.10: the last part
    # alone took ``BlockService.ask`` for ``HubBlockPorts.ask``, another class)
    if "." not in want and _kind_of(path) != "json":
        tail = [s for s in every if s.name.rsplit(".", 1)[-1] == want]
        if tail:
            return tail, []
    names = list(dict.fromkeys(s.name for s in every))
    close = difflib.get_close_matches(want, names, n=NEAREST, cutoff=0.4)
    if len(close) < NEAREST:
        low = want.rsplit(".", 1)[-1].lower()
        close += [n for n in names if low and low in n.lower() and n not in close]
    return [], close[:NEAREST]
