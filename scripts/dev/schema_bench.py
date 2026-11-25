#!/usr/bin/env python3
"""Bench for plan step 1.12: does ``response_format: json_schema`` hold with thinking on and off,
per model family, on the llama.cpp build the nodes run? Run on the hub host (token as hubapi.py):

    schema_bench.py MODEL[@NODE] [MODEL ...] [--repeat 2]

For each model: json_object vs json_schema, thinking off / on. Prints HTTP status, seconds,
completion tokens, finish reason, whether the answer is JSON and whether it has the schema's
required keys. Nothing is written anywhere.
"""

import json
import ssl
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from hubapi import _token  # noqa: E402

URL = "https://127.0.0.1:8443/v1/chat/completions"
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "symbol": {"type": "string"},
                    "where": {"enum": ["in", "before", "after", "end"]},
                    "do": {"type": "string"},
                },
                "required": ["path", "symbol", "where", "do"],
            },
        },
        "test": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "do": {"type": "string"}},
            "required": ["path", "do"],
        },
    },
    "required": ["summary", "steps", "test"],
}
CODE = '''\
### hub/src/topcoat_hub/text_tools.py
def parse(text):
    """The words of a text."""
    return text.split()


class Counter:
    def count(self, text):
        return len(parse(text))
'''
PROMPT = (
    "Plan the fix as steps, one place each; name each place by its symbol (function, "
    "Class.method). Problem: Counter.count crashes on None; it should count 0 words. The code:\n"
    + CODE
    + '\nAnswer with ONE JSON object: {"summary": "...", "steps": [{"path", "symbol", "where", '
    '"do"}], "test": {"path": "hub/tests/test_<name>.py", "do": "..."}}'
)


def ask(model: str, fmt: str, think: bool) -> dict:
    payload = {
        "model": model,
        "stream": False,
        "temperature": 0.1,
        "max_tokens": 2500,
        "messages": [{"role": "user", "content": PROMPT}],
        "chat_template_kwargs": {"enable_thinking": think},
    }
    if fmt == "json_object":
        payload["response_format"] = {"type": "json_object"}
    else:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "plan", "schema": SCHEMA},
        }
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    t = time.time()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=600) as r:
            data, status = json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return {"status": e.code, "s": round(time.time() - t, 1), "err": e.read()[:200].decode()}
    msg = (data.get("choices") or [{}])[0]
    text = (msg.get("message") or {}).get("content") or ""
    try:
        doc = json.loads(text)
        ok_json = isinstance(doc, dict)
    except ValueError:
        doc, ok_json = None, False
    has = ok_json and all(k in doc for k in SCHEMA["required"])
    steps_ok = has and all(
        isinstance(s, dict) and all(k in s for k in ("path", "symbol", "where", "do"))
        for s in doc.get("steps") or [None]
    )
    return {
        "status": status,
        "s": round(time.time() - t, 1),
        "tokens": (data.get("usage") or {}).get("completion_tokens"),
        "finish": msg.get("finish_reason"),
        "json": ok_json,
        "shape": bool(steps_ok),
        "head": text[:80].replace("\n", " "),
    }


def main(argv: list[str]) -> int:
    repeat = 1
    if "--repeat" in argv:
        i = argv.index("--repeat")
        repeat = int(argv[i + 1])
        del argv[i : i + 2]
    for model in argv:
        for fmt in ("json_object", "json_schema"):
            for think in (False, True):
                for _ in range(repeat):
                    r = ask(model, fmt, think)
                    print(model, fmt, "think" if think else "nothink", json.dumps(r, ensure_ascii=False))
                    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
