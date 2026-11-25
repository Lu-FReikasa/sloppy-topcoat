"""The measurement script for small development tasks (scripts/dev/agent_bench.py): its checks
are right (they fail on the untouched project, pass on the reference solution) and the loop works
against a scripted OpenAI-compatible server."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "agent_bench.py"
spec = importlib.util.spec_from_file_location("agent_bench", SCRIPT)
assert spec and spec.loader
bench = importlib.util.module_from_spec(spec)
sys.modules["agent_bench"] = bench
spec.loader.exec_module(bench)


def test_every_check_fails_untouched_and_passes_with_the_reference_solution() -> None:
    assert bench.selftest() == 0


def test_the_loop_runs_tools_and_the_hidden_check_decides() -> None:
    steps = iter(
        [
            {"tool_calls": [_call("read_file", {"path": "pager/paginate.py"})]},
            {
                "tool_calls": [
                    _call(
                        "replace_in_file",
                        {
                            "path": "pager/paginate.py",
                            "old": "page * size",
                            "new": "(page - 1) * size",
                        },
                    )
                ]
            },
            {"tool_calls": [_call("run_tests", {})]},
            {"content": "Fixed."},
        ]
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"choices": [{"message": {"role": "assistant", **next(steps)}}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        args = argparse.Namespace(
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="fake",
            api_key_env=None,
            max_turns=8,
        )
        task = next(t for t in bench.TASKS if t.key == "off-by-one")
        result = bench.attempt(task, args, None)
    finally:
        server.shutdown()
    assert result["solved"] and result["ended"] == "answered" and result["tool_calls"] == 3


def test_a_model_that_never_stops_calling_tools_ends_at_the_turn_limit() -> None:
    class Loop(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            msg = {"role": "assistant", "tool_calls": [_call("list_files", {})]}
            body = json.dumps({"choices": [{"message": msg}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Loop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        args = argparse.Namespace(
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="fake",
            api_key_env=None,
            max_turns=10,
        )
        result = bench.attempt(bench.TASKS[0], args, None)
    finally:
        server.shutdown()
    assert not result["solved"] and result["ended"] == "repeated_calls"


def _call(name: str, args: dict) -> dict:
    return {
        "id": "c",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }
