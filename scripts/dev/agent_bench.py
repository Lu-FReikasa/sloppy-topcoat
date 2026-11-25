#!/usr/bin/env python3
"""Measure how well a chat model handles small development tasks (E6-01, before S1).

Ten tiny tasks (rename, small bug with a test, i18n key, ...). Each task is a few files in a
scratch directory. The model gets a few read/write tools and a test command, works until it
answers without a tool call (or runs out of turns), then a hidden check decides: solved or not.
Standard library only; the model is any OpenAI-compatible endpoint (a llama-server, or the hub's
/v1 route with an API key).

    scripts/dev/agent_bench.py --selftest
    scripts/dev/agent_bench.py --base-url http://127.0.0.1:8080/v1 --model gemma --out gemma.json
    scripts/dev/agent_bench.py --base-url https://hub.example.lan:8443/v1 --model qwen3-30b \\
        --api-key-env TOPCOAT_API_KEY --insecure --only rename,off-by-one

Nothing here touches the repository: every run happens in a temporary directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Task:
    key: str
    prompt: str
    files: dict[str, str]
    solution: dict[str, str]  # files the reference solution writes (selftest only)
    check: Callable[[Path], tuple[bool, str]]
    delete: tuple[str, ...] = field(default=())


def run(cmd: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return p.returncode, (p.stdout + p.stderr)[-3000:]


def unittests(root: Path) -> tuple[bool, str]:
    code, out = run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], root)
    return code == 0, out


def read(root: Path, rel: str) -> str:
    p = root / rel
    return p.read_text() if p.exists() else ""


# --- the tasks ------------------------------------------------------------------------------

T_RENAME_FILES = {
    "shop/__init__.py": "",
    "shop/prices.py": "def calc_total(items):\n    return sum(i['price'] * i['qty'] for i in items)\n",
    "shop/invoice.py": (
        "from shop.prices import calc_total\n\n\n"
        "def invoice(items):\n    return f'Total: {calc_total(items):.2f}'\n"
    ),
    "shop/cli.py": (
        "from shop.prices import calc_total\n\n\ndef main(items):\n    print(calc_total(items))\n"
    ),
    "tests/test_prices.py": (
        "import unittest\nfrom shop.prices import calc_total\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_total(self):\n"
        "        self.assertEqual(calc_total([{'price': 2.5, 'qty': 2}]), 5.0)\n"
    ),
}


def check_rename(root: Path) -> tuple[bool, str]:
    left = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if "calc_total" in p.read_text() and "tests" not in p.parts
    ]
    if left:
        return False, f"old name still in {left}"
    if "compute_total" not in read(root, "shop/prices.py"):
        return False, "compute_total not defined"
    # the test file may keep or change its import; the library code must work
    code, out = run(
        [
            sys.executable,
            "-c",
            "from shop.invoice import invoice;print(invoice([{'price': 2.5, 'qty': 2}]))",
        ],
        root,
    )
    return code == 0 and "5.00" in out, out


T_OFF_BY_ONE_FILES = {
    "pager/__init__.py": "",
    "pager/paginate.py": (
        "def paginate(items, page, size):\n"
        '    """Page numbers start at 1."""\n'
        "    start = page * size\n"
        "    return items[start:start + size]\n"
    ),
    "tests/test_paginate.py": (
        "import unittest\nfrom pager.paginate import paginate\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_first_page(self):\n"
        "        self.assertEqual(paginate(list(range(10)), 1, 3), [0, 1, 2])\n"
    ),
}


def check_off_by_one(root: Path) -> tuple[bool, str]:
    code, out = run(
        [
            sys.executable,
            "-c",
            "from pager.paginate import paginate as p; d=list(range(10));"
            "assert p(d,1,3)==[0,1,2] and p(d,2,3)==[3,4,5] and p(d,4,3)==[9] and p(d,5,3)==[]",
        ],
        root,
    )
    return code == 0, out


T_I18N_FILES = {
    "i18n/en.json": '{\n  "save": "Save",\n  "cancel": "Cancel"\n}\n',
    "i18n/de.json": '{\n  "save": "Speichern",\n  "cancel": "Abbrechen"\n}\n',
    "tests/test_parity.py": (
        "import json, unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_same_keys(self):\n"
        "        en = json.load(open('i18n/en.json'))\n"
        "        de = json.load(open('i18n/de.json'))\n"
        "        self.assertEqual(set(en), set(de))\n"
    ),
}


def check_i18n(root: Path) -> tuple[bool, str]:
    try:
        en = json.loads(read(root, "i18n/en.json"))
        de = json.loads(read(root, "i18n/de.json"))
    except ValueError as exc:
        return False, f"invalid JSON: {exc}"
    ok = (
        en.get("delete") not in (None, "")
        and de.get("delete") not in (None, "")
        and en.get("save") == "Save"
        and de.get("save") == "Speichern"
        and set(en) == set(de)
        and "lösch" in de["delete"].lower()
    )
    return ok, f"en={en} de={de}"


T_FLAG_FILES = {
    "tool/__init__.py": "",
    "tool/cli.py": (
        "import argparse\n\n\n"
        "def build_parser():\n"
        "    p = argparse.ArgumentParser(prog='tool')\n"
        "    p.add_argument('name')\n"
        "    return p\n\n\n"
        "def greet(args):\n"
        "    return f'Hello {args.name}'\n"
    ),
    "tests/test_cli.py": (
        "import unittest\nfrom tool.cli import build_parser, greet\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_greet(self):\n"
        "        self.assertEqual(greet(build_parser().parse_args(['Ann'])), 'Hello Ann')\n"
    ),
}


def check_flag(root: Path) -> tuple[bool, str]:
    code, out = run(
        [
            sys.executable,
            "-c",
            "from tool.cli import build_parser, greet;"
            "a=build_parser().parse_args(['Ann','--shout']);"
            "assert greet(a)=='HELLO ANN', greet(a);"
            "b=build_parser().parse_args(['Ann']);"
            "assert greet(b)=='Hello Ann', greet(b)",
        ],
        root,
    )
    return code == 0, out


T_DURATION_FILES = {
    "timeutil/__init__.py": "",
    "timeutil/duration.py": (
        "import re\n\n\n"
        "def parse_duration(text):\n"
        '    """\'1h30m\' -> 5400 seconds; units h, m, s."""\n'
        "    total = 0\n"
        "    for number, unit in re.findall(r'(\\d+)([hms])', text):\n"
        "        total += int(number) * {'h': 360, 'm': 60, 's': 1}[unit]\n"
        "    return total\n"
    ),
    "tests/test_duration.py": (
        "import unittest\nfrom timeutil.duration import parse_duration\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_minutes(self):\n"
        "        self.assertEqual(parse_duration('2m'), 120)\n"
    ),
}


def check_duration(root: Path) -> tuple[bool, str]:
    code, out = run(
        [
            sys.executable,
            "-c",
            "from timeutil.duration import parse_duration as p;"
            "assert p('1h30m')==5400 and p('45s')==45 and p('2h')==7200 and p('')==0",
        ],
        root,
    )
    return code == 0, out


T_TEST_FILES = {
    "words/__init__.py": "",
    "words/count.py": ("def count_words(text):\n    return len(text.split())\n"),
}


def check_add_test(root: Path) -> tuple[bool, str]:
    tests = [p for p in (root / "tests").glob("test*.py")] if (root / "tests").exists() else []
    if not tests:
        return False, "no test file under tests/"
    body = "\n".join(p.read_text() for p in tests)
    if body.count("def test_") < 2 or "count_words" not in body:
        return False, "fewer than two test methods or count_words not used"
    ok, out = unittests(root)
    if not ok:
        return False, out
    # the tests must catch a broken implementation
    broken = root / "words" / "count.py"
    original = broken.read_text()
    broken.write_text("def count_words(text):\n    return len(text)\n")
    try:
        caught, _ = unittests(root)
    finally:
        broken.write_text(original)
    return (not caught), "tests did not fail on a broken implementation" if caught else "ok"


T_LOOKUP_FILES = {
    "lab/__init__.py": "",
    "lab/inventory.py": (
        "HOSTS = {'node-amd': 'gpu', 'node-laptop': 'cpu'}\n\n\n"
        "def kind_of(host):\n"
        "    return HOSTS.get(host)\n"
    ),
    "tests/test_inventory.py": (
        "import unittest\nfrom lab.inventory import kind_of\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_known(self):\n"
        "        self.assertEqual(kind_of('node-amd'), 'gpu')\n"
    ),
}


def check_lookup(root: Path) -> tuple[bool, str]:
    code, out = run(
        [
            sys.executable,
            "-c",
            "from lab.inventory import kind_of as k;"
            "assert k('NODE-AMD')=='gpu' and k('Node-Laptop')=='cpu' and k('node-amd')=='gpu';"
            "assert k('other') is None",
        ],
        root,
    )
    return code == 0, out


T_NONE_FILES = {
    "text/__init__.py": "",
    "text/shorten.py": (
        "def shorten(text, limit):\n"
        "    if len(text) <= limit:\n"
        "        return text\n"
        "    return text[:limit - 1] + '…'\n"
    ),
    "tests/test_shorten.py": (
        "import unittest\nfrom text.shorten import shorten\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_long(self):\n"
        "        self.assertEqual(shorten('abcdef', 4), 'abc…')\n"
    ),
}


def check_none(root: Path) -> tuple[bool, str]:
    code, out = run(
        [
            sys.executable,
            "-c",
            "from text.shorten import shorten as s;"
            "assert s(None,5)=='' and s('abcdef',4)=='abc…' and s('ab',4)=='ab'",
        ],
        root,
    )
    return code == 0, out


T_README_FILES = {
    "README.md": (
        "# backup-tool\n\nRun a backup:\n\n    backup --target /mnt/disk --dry-run\n\n"
        "Options: `--target`, `--dry-run`.\n"
    ),
    "backup/__init__.py": "",
    "backup/cli.py": (
        "import argparse\n\n\n"
        "def build_parser():\n"
        "    p = argparse.ArgumentParser(prog='backup')\n"
        "    p.add_argument('--destination', required=True)\n"
        "    p.add_argument('--check-only', action='store_true')\n"
        "    return p\n"
    ),
    "tests/test_cli.py": (
        "import unittest\nfrom backup.cli import build_parser\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_parse(self):\n"
        "        self.assertTrue(build_parser().parse_args(['--destination', 'x']).destination)\n"
    ),
}


def check_readme(root: Path) -> tuple[bool, str]:
    text = read(root, "README.md")
    docs_ok = (
        "--destination" in text
        and "--check-only" in text
        and "--target" not in text
        and "--dry-run" not in text
    )
    code_unchanged = read(root, "backup/cli.py") == T_README_FILES["backup/cli.py"]
    tests_ok, out = unittests(root)
    if not code_unchanged:
        return False, "backup/cli.py was changed"
    return docs_ok and tests_ok, out if not docs_ok else "ok"


T_DEAD_FILES = {
    "util/__init__.py": "",
    "util/helpers.py": (
        "import os\nimport json\n\n\n"
        "def slug(text):\n"
        "    return text.lower().replace(' ', '-')\n\n\n"
        "def _old_slug_v1(text):\n"
        "    return text.replace(' ', '_')\n"
    ),
    "tests/test_helpers.py": (
        "import unittest\nfrom util.helpers import slug\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_slug(self):\n"
        "        self.assertEqual(slug('A B'), 'a-b')\n"
    ),
}


def check_dead(root: Path) -> tuple[bool, str]:
    text = read(root, "util/helpers.py")
    ok, out = unittests(root)
    gone = "_old_slug_v1" not in text and "import os" not in text and "import json" not in text
    if not gone:
        return False, "dead code still there"
    return ok and "def slug" in text, out


TASKS: list[Task] = [
    Task(
        "rename",
        "Rename the function `calc_total` to `compute_total` everywhere in the library code "
        "(shop/). Keep the behaviour. Update the test import too.",
        T_RENAME_FILES,
        {
            "shop/prices.py": T_RENAME_FILES["shop/prices.py"].replace(
                "calc_total", "compute_total"
            ),
            "shop/invoice.py": T_RENAME_FILES["shop/invoice.py"].replace(
                "calc_total", "compute_total"
            ),
            "shop/cli.py": T_RENAME_FILES["shop/cli.py"].replace("calc_total", "compute_total"),
            "tests/test_prices.py": T_RENAME_FILES["tests/test_prices.py"].replace(
                "calc_total", "compute_total"
            ),
        },
        check_rename,
    ),
    Task(
        "off-by-one",
        "`paginate` returns the wrong slice: page 1 should be the first `size` items. Fix the bug.",
        T_OFF_BY_ONE_FILES,
        {
            "pager/paginate.py": T_OFF_BY_ONE_FILES["pager/paginate.py"].replace(
                "page * size", "(page - 1) * size"
            )
        },
        check_off_by_one,
    ),
    Task(
        "i18n-key",
        'Add a new key `delete` to both catalogues in i18n/ (English "Delete", German '
        '"Löschen"). Keep the other keys as they are; the key sets must stay equal.',
        T_I18N_FILES,
        {
            "i18n/en.json": '{\n  "save": "Save",\n  "cancel": "Cancel",\n  "delete": "Delete"\n}\n',
            "i18n/de.json": (
                '{\n  "save": "Speichern",\n  "cancel": "Abbrechen",\n  "delete": "Löschen"\n}\n'
            ),
        },
        check_i18n,
    ),
    Task(
        "cli-flag",
        "Add a `--shout` flag to the command line in tool/cli.py. With it, `greet` returns the "
        "greeting in upper case ('HELLO ANN'); without it nothing changes.",
        T_FLAG_FILES,
        {
            "tool/cli.py": T_FLAG_FILES["tool/cli.py"]
            .replace(
                "    p.add_argument('name')\n",
                "    p.add_argument('name')\n    p.add_argument('--shout', action='store_true')\n",
            )
            .replace(
                "    return f'Hello {args.name}'\n",
                "    text = f'Hello {args.name}'\n    return text.upper() if args.shout else text\n",
            )
        },
        check_flag,
    ),
    Task(
        "duration-bug",
        "`parse_duration('1h30m')` gives the wrong number of seconds. Find the bug and fix it.",
        T_DURATION_FILES,
        {"timeutil/duration.py": T_DURATION_FILES["timeutil/duration.py"].replace("360", "3600")},
        check_duration,
    ),
    Task(
        "add-test",
        "Write tests for `count_words` in words/count.py under tests/ (at least two test "
        "methods, unittest style, including an empty string). Do not change the library code.",
        T_TEST_FILES,
        {
            "tests/test_count.py": (
                "import unittest\nfrom words.count import count_words\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_two(self):\n        self.assertEqual(count_words('a b'), 2)\n"
                "    def test_empty(self):\n        self.assertEqual(count_words(''), 0)\n"
            )
        },
        check_add_test,
    ),
    Task(
        "case-lookup",
        "`kind_of` should find hosts regardless of upper or lower case ('NODE-AMD' -> 'gpu'). "
        "Unknown hosts still give None.",
        T_LOOKUP_FILES,
        {
            "lab/inventory.py": T_LOOKUP_FILES["lab/inventory.py"].replace(
                "HOSTS.get(host)", "HOSTS.get(host.lower())"
            )
        },
        check_lookup,
    ),
    Task(
        "none-input",
        "`shorten(None, 5)` crashes. It should return an empty string for None; everything else "
        "stays as it is.",
        T_NONE_FILES,
        {
            "text/shorten.py": T_NONE_FILES["text/shorten.py"].replace(
                "    if len(text) <= limit:",
                "    if text is None:\n        return ''\n    if len(text) <= limit:",
            )
        },
        check_none,
    ),
    Task(
        "readme-sync",
        "The README describes options that no longer exist. Update the README so it matches "
        "backup/cli.py (do not change the code).",
        T_README_FILES,
        {
            "README.md": (
                "# backup-tool\n\nRun a backup:\n\n    backup --destination /mnt/disk --check-only\n\n"
                "Options: `--destination`, `--check-only`.\n"
            )
        },
        check_readme,
    ),
    Task(
        "dead-code",
        "In util/helpers.py remove the unused imports and the unused function `_old_slug_v1`. "
        "`slug` must keep working.",
        T_DEAD_FILES,
        {"util/helpers.py": ("def slug(text):\n    return text.lower().replace(' ', '-')\n")},
        check_dead,
    ),
]


# --- the harness ----------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(props)},
        },
    }
    for name, desc, props in (
        ("list_files", "List all files of the project.", {}),
        ("read_file", "Read a file.", {"path": {"type": "string"}}),
        (
            "write_file",
            "Create or overwrite a file with the full new content.",
            {"path": {"type": "string"}, "content": {"type": "string"}},
        ),
        (
            "replace_in_file",
            "Replace one exact piece of text in a file (it must occur exactly once).",
            {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
        ),
        (
            "search",
            "Find files whose text contains the given string.",
            {"text": {"type": "string"}},
        ),
        ("run_tests", "Run the project's tests (python -m unittest).", {}),
    )
]

SYSTEM = (
    "You are a careful developer working in a small Python project. Use the tools to look at the "
    "files, make the change and run the tests. When you are done, answer with one short "
    "sentence. Do not ask questions; do not change more than the task needs."
)


def safe(root: Path, rel: str) -> Path | None:
    p = (root / rel).resolve()
    return p if p == root.resolve() or root.resolve() in p.parents else None


def call_tool(root: Path, name: str, args: dict) -> str:
    try:
        if name == "list_files":
            return "\n".join(
                sorted(
                    str(p.relative_to(root))
                    for p in root.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts
                )
            )
        if name == "search":
            hits = [
                str(p.relative_to(root))
                for p in sorted(root.rglob("*"))
                if p.is_file()
                and "__pycache__" not in p.parts
                and str(args["text"]) in p.read_text(errors="ignore")
            ]
            return "\n".join(hits) or "no match"
        if name == "run_tests":
            return unittests(root)[1]
        path = safe(root, str(args["path"]))
        if path is None:
            return "error: path outside the project"
        if name == "read_file":
            return path.read_text() if path.is_file() else "error: no such file"
        if name == "write_file":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(args["content"]))
            return "written"
        if name == "replace_in_file":
            if not path.is_file():
                return "error: no such file"
            text = path.read_text()
            if text.count(str(args["old"])) != 1:
                return f"error: the text occurs {text.count(str(args['old']))} times, need 1"
            path.write_text(text.replace(str(args["old"]), str(args["new"])))
            return "replaced"
    except (KeyError, TypeError, OSError) as exc:
        return f"error: {exc}"
    return f"error: unknown tool {name}"


def chat(base: str, model: str, key: str | None, messages: list, ctx: ssl.SSLContext | None):
    body = json.dumps(
        {"model": model, "messages": messages, "tools": TOOLS, "temperature": 0.2, "stream": False}
    ).encode()
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {key}"} if key else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=600, context=ctx) as r:  # noqa: S310
        return json.load(r)["choices"][0]["message"]


def seed(task: Task, root: Path) -> None:
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "__init__.py").write_text("")
    for rel, text in task.files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def attempt(task: Task, args: argparse.Namespace, ctx) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"bench-{task.key}-"))
    seed(task, root)
    key = os.environ.get(args.api_key_env) if args.api_key_env else None
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": task.prompt},
    ]
    turns = calls = repeats = 0
    seen: set[str] = set()
    ended = "max_turns"
    started = time.time()
    try:
        while turns < args.max_turns:
            turns += 1
            msg = chat(args.base_url, args.model, key, messages, ctx)
            messages.append({k: v for k, v in msg.items() if v is not None})
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                ended = "answered"
                break
            for tc in tool_calls:
                calls += 1
                fn = tc["function"]
                try:
                    a = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    a = {}
                sig = fn["name"] + json.dumps(a, sort_keys=True)
                if sig in seen and fn["name"] not in ("run_tests",):
                    repeats += 1
                seen.add(sig)
                result = call_tool(root, fn["name"], a if isinstance(a, dict) else {})
                messages.append(
                    {"role": "tool", "tool_call_id": tc.get("id", ""), "content": result[:4000]}
                )
            if repeats >= 4:
                ended = "repeated_calls"
                break
        ok, why = task.check(root)
    except Exception as exc:  # network or protocol trouble counts as a failed run
        ok, why, ended = False, f"{type(exc).__name__}: {exc}", "error"
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return {
        "task": task.key,
        "solved": ok,
        "ended": ended,
        "turns": turns,
        "tool_calls": calls,
        "repeats": repeats,
        "seconds": round(time.time() - started, 1),
        "note": "" if ok else why[-300:],
    }


def selftest() -> int:
    """Every check fails on the untouched project and passes on the reference solution."""
    bad = 0
    for task in TASKS:
        root = Path(tempfile.mkdtemp(prefix=f"bench-self-{task.key}-"))
        try:
            seed(task, root)
            before, _ = task.check(root)
            for rel, text in task.solution.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text)
            after, why = task.check(root)
            good = (not before) and after
            bad += 0 if good else 1
            print(
                f"{'ok  ' if good else 'FAIL'} {task.key}: untouched={before} solved={after} {'' if good else why}"
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base-url", help="OpenAI-compatible base, e.g. http://127.0.0.1:8080/v1")
    ap.add_argument("--model", default="default")
    ap.add_argument("--api-key-env", help="name of the environment variable holding the key")
    ap.add_argument("--insecure", action="store_true", help="do not verify the TLS certificate")
    ap.add_argument("--max-turns", type=int, default=15)
    ap.add_argument("--repeat", type=int, default=1, help="attempts per task")
    ap.add_argument("--only", help="comma separated task keys")
    ap.add_argument("--out", help="write the results as JSON")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.base_url:
        ap.error("--base-url is required (or use --selftest)")
    ctx = ssl._create_unverified_context() if args.insecure else None  # noqa: S323
    wanted = set(args.only.split(",")) if args.only else None
    results = []
    for task in TASKS:
        if wanted and task.key not in wanted:
            continue
        for _ in range(args.repeat):
            r = attempt(task, args, ctx)
            results.append(r)
            print(
                f"{'solved' if r['solved'] else 'failed':6} {r['task']:14} {r['ended']:15} "
                f"turns={r['turns']:2} calls={r['tool_calls']:2} {r['seconds']}s {r['note'][:80]}"
            )
    solved = sum(r["solved"] for r in results)
    print(f"\n{args.model}: {solved}/{len(results)} solved")
    if args.out:
        Path(args.out).write_text(json.dumps({"model": args.model, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
