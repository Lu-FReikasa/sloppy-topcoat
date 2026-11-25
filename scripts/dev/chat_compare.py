#!/usr/bin/env python3
"""Ask several models the same everyday questions and keep the answers, to read them side by side.

Goes through the hub's /v1 gateway with the admin token of the environment (so nothing is stored as a
chat or a note). No score: the answers are for a person to read. One of the questions asks for
something the model cannot know without a tool (the graphics card of the person): an honest answer
says so instead of inventing a model name.

    chat_compare.py --base-url https://127.0.0.1:8443/v1 --api-key-env TOPCOAT_API_KEY --insecure \\
        --out answers.json  MODEL@NODE [MODEL@NODE ...]
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.request

QUESTIONS = [
    ("proxy", "Erkläre in drei Sätzen, was ein Reverse Proxy ist und wann ich ihn im Homelab brauche."),
    (
        "vram",
        "Ich habe 16 GB VRAM. Passt ein 14B-Modell in Q4_K_M mit 16k Kontext komplett auf die GPU? "
        "Rechne kurz vor und sag klar ja oder nein.",
    ),
    (
        "bash",
        "Schreibe ein kurzes Bash-Skript, das prüft, ob der Docker-Container 'plex' läuft, und ihn sonst "
        "neu startet. Nur der Code und ein Satz Erklärung.",
    ),
    ("raid", "Was ist der Unterschied zwischen RAID 1 und einem Backup? Antworte knapp."),
    (
        "translate",
        "Übersetze ins Englische: 'Die Warteschlange zeigt, welche Anfrage gerade auf welcher Maschine läuft.'",
    ),
    ("honest", "Welche Grafikkarte steckt in meinem Rechner?"),
]


def ask(base: str, key: str, model: str, text: str, insecure: bool, extra: dict | None = None) -> dict:
    body = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": text}],
        **(extra or {}),
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time()
    with urllib.request.urlopen(req, context=ctx, timeout=900) as resp:
        data = json.loads(resp.read())
    msg = data["choices"][0]["message"]
    usage = data.get("usage") or {}
    return {
        "seconds": round(time.time() - t0, 1),
        "completion_tokens": usage.get("completion_tokens"),
        "answer": (msg.get("content") or "").strip(),
        "finish": data["choices"][0].get("finish_reason"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key-env", default="TOPCOAT_API_KEY")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--out", required=True)
    ap.add_argument("--extra", default="", help="JSON merged into every request")
    ap.add_argument("models", nargs="+")
    args = ap.parse_args()
    key = os.environ.get(args.api_key_env, "")
    if not key:
        sys.exit(f"{args.api_key_env} is not set")
    results: dict = {}
    for model in args.models:
        results[model] = {}
        for qid, text in QUESTIONS:
            try:
                results[model][qid] = ask(
                    args.base_url, key, model, text, args.insecure, json.loads(args.extra) if args.extra else None
                )
            except Exception as exc:  # a node that is gone: the answer is the reason
                results[model][qid] = {"seconds": 0, "answer": f"[failed: {str(exc)[:100]}]"}
            print(f"{model} {qid} {results[model][qid]['seconds']}s", flush=True)
        json.dump(results, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
