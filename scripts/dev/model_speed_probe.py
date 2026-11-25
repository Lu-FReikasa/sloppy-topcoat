#!/usr/bin/env python3
"""Speed of one model on one node, through the hub's /v1 route (run on the hub host).

    model_speed_probe.py MODEL NODE

Three requests: a short one (this includes loading the model), a prompt of about 5000 tokens with a one-number
answer (prefill speed) and a request for 300 tokens of text (generation speed). Prints one line. Measured on
2026-11-04 on an 8 GB card: MoE models (gemma-26B-A4B, Qwen3-30B-A3B, gpt-oss-20b) generate 33 to 41 tokens per
second, a dense 27B at Q2 1.5 (see hand-over 2026-11-04-live-report-tests).

Unload the model afterwards. The hub's gateway answers 504 after its action timeout (5 minutes): a model that
cannot prefill 5000 tokens in that time is too slow for an agent anyway.
Environment: TOPCOAT_ENV_FILE (default ~/topcoat/deploy/hub.env), TOPCOAT_HUB_URL (default https://127.0.0.1:8443).
"""

import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path

ENV = Path(os.environ.get("TOPCOAT_ENV_FILE", "~/topcoat/deploy/hub.env")).expanduser()
URL = os.environ.get("TOPCOAT_HUB_URL", "https://127.0.0.1:8443") + "/v1/chat/completions"
CTX = ssl.create_default_context()
CTX.check_hostname, CTX.verify_mode = False, ssl.CERT_NONE


def _token() -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith("TOPCOAT_DEV_ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("TOPCOAT_DEV_ADMIN_TOKEN not found in the env file")


def ask(model: str, prompt: str, max_tokens: int) -> tuple[float, dict]:
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
         "temperature": 0.2}
    ).encode()
    req = urllib.request.Request(URL, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Content-Type", "application/json")
    start = time.time()
    with urllib.request.urlopen(req, context=CTX, timeout=1500) as r:  # noqa: S310
        data = json.loads(r.read())
    return time.time() - start, data


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    target = f"{argv[1]}@{argv[2]}"
    filler = " ".join(
        f"Item {i}: the quick brown fox jumps over the lazy dog number {i}." for i in range(260)
    )
    t_load, _ = ask(target, "Say OK.", 4)
    t_pre, first = ask(target, filler + "\n\nHow many items are listed? Answer with one number.", 8)
    t_gen, second = ask(target, "Write about 300 words on why the sky is blue.", 300)
    prompt_tokens, new_tokens = first["usage"]["prompt_tokens"], second["usage"]["completion_tokens"]
    print(
        f"{target} load+first={t_load:.0f}s prefill={prompt_tokens}tok/{t_pre:.1f}s="
        f"{prompt_tokens / max(t_pre, 0.1):.0f}tok/s gen={new_tokens}tok/{t_gen:.1f}s="
        f"{new_tokens / max(t_gen, 0.1):.1f}tok/s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
