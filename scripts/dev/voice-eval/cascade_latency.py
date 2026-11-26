#!/usr/bin/env python3
"""Time to first sound of the streaming cascade (D127, issue #59), measured step by step.

The cascade: the person stops speaking -> speech to text (whisper.cpp) -> the text agent streams
its answer (any OpenAI-compatible endpoint, e.g. the hub's gateway) -> the first sentence goes to
text to speech (Piper) -> the first sound plays. This script measures each step on this machine
and adds them up; it uses only the standard library and the two command-line programs.

    python3 cascade_latency.py --whisper-cli PATH --whisper-model ggml-base.bin \\
        --piper PATH --voice de_DE-thorsten-medium.onnx \\
        [--llm-url https://HUB:8443/v1/chat/completions --llm-model MODEL] [--ca-file ca.pem]

The LLM token comes from the environment (TOPCOAT_TOKEN), never from the command line. Without
--llm-url the LLM step is left out. The questions are spoken by Piper first, so the run needs no
microphone; the transcripts are compared with the questions (word error rate).
"""

import argparse
import json
import os
import ssl
import statistics
import subprocess
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

QUESTIONS = [
    "Wie spät ist es?",
    "Was steht heute in meinem Kalender?",
    "Kannst du mir kurz erklären, warum der Knoten heute Nacht offline war?",
    "Fasse bitte die drei wichtigsten Punkte aus dem Bericht von gestern zusammen und sag mir, "
    "was ich als Nächstes tun sollte.",
]


def run(cmd: list[str], *, stdin: bytes | None = None, lib_dir: str | None = None) -> float:
    env = dict(os.environ)
    if lib_dir:
        env["LD_LIBRARY_PATH"] = lib_dir
    t0 = time.perf_counter()
    subprocess.run(cmd, input=stdin, env=env, check=True, capture_output=True)
    return time.perf_counter() - t0


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def speak(args: argparse.Namespace, text: str, out: Path) -> float:
    piper = Path(args.piper)
    cmd = [str(piper), "--model", args.voice, "--output_file", str(out)]
    espeak = piper.parent / "espeak-ng-data"
    if espeak.is_dir():
        cmd += ["--espeak_data", str(espeak)]
    return run(cmd, stdin=(text + "\n").encode(), lib_dir=str(piper.parent))


def transcribe(args: argparse.Namespace, wav: Path, tmp: Path) -> tuple[float, str, str]:
    cli = Path(args.whisper_cli)
    out = tmp / "stt"
    cmd = [str(cli), "-m", args.whisper_model, "-f", str(wav), "-l", args.language,
           "-oj", "-of", str(out), "-nt", "-np", "-t", str(args.threads)]
    secs = run(cmd, lib_dir=str(cli.parent))
    res = json.loads((tmp / "stt.json").read_text(encoding="utf-8"))
    text = " ".join(s["text"].strip() for s in res["transcription"]).strip()
    return secs, text, res["result"]["language"]


def first_sentence(args: argparse.Namespace, question: str) -> tuple[float, float, str]:
    """Seconds to the first token and to the end of the first sentence of a streamed answer."""
    body = json.dumps({
        "model": args.llm_model,
        "stream": True,
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": "Antworte kurz und gesprochen, ohne Aufzählungen."},
            {"role": "user", "content": question},
        ],
    }).encode()
    req = urllib.request.Request(args.llm_url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token := os.environ.get("TOPCOAT_TOKEN"):
        req.add_header("Authorization", "Bearer " + token)
    ctx = ssl.create_default_context(cafile=args.ca_file) if args.ca_file else None
    t0 = time.perf_counter()
    first = None
    text = ""
    with urllib.request.urlopen(req, context=ctx, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            try:
                delta = json.loads(line[5:])["choices"][0]["delta"].get("content") or ""
            except (ValueError, KeyError, IndexError):
                continue
            if delta and first is None:
                first = time.perf_counter() - t0
            text += delta
            if any(p in text for p in ".!?") and len(text.strip()) > 8:
                break
    sentence = text.strip()
    for p in ".!?":
        if p in sentence:
            sentence = sentence[: sentence.index(p) + 1]
            break
    return first or 0.0, time.perf_counter() - t0, sentence


def wer(ref: str, hyp: str) -> float:
    r = [w.strip(".,?!").lower() for w in ref.split()]
    h = [w.strip(".,?!").lower() for w in hyp.split()]
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[len(h)] / max(len(r), 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--whisper-cli", required=True)
    ap.add_argument("--whisper-model", required=True)
    ap.add_argument("--piper", required=True)
    ap.add_argument("--voice", required=True, help="Piper .onnx (its .onnx.json next to it)")
    ap.add_argument("--language", default="auto")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--llm-url")
    ap.add_argument("--llm-model", default="")
    ap.add_argument("--ca-file")
    ap.add_argument("--rounds", type=int, default=2)
    args = ap.parse_args()

    rows = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for q in QUESTIONS:
            wav = tmp / "q.wav"
            speak(args, q, wav)
            secs = wav_seconds(wav)
            stt = []
            for _ in range(args.rounds):
                s, text, lang = transcribe(args, wav, tmp)
                stt.append(s)
            row = {"question_s": round(secs, 1), "stt_s": round(statistics.median(stt), 2),
                   "language": lang, "wer": round(wer(q, text), 2), "transcript": text}
            sentence = "Das mache ich gern, einen Moment bitte."
            if args.llm_url:
                ttft, t_sent, sentence = first_sentence(args, text)
                row.update(llm_first_token_s=round(ttft, 2), llm_first_sentence_s=round(t_sent, 2))
            tts = [speak(args, sentence, tmp / "a.wav") for _ in range(args.rounds)]
            row["tts_first_sentence_s"] = round(statistics.median(tts), 2)
            row["first_sound_s"] = round(row["stt_s"] + row.get("llm_first_sentence_s", 0) + row["tts_first_sentence_s"], 2)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False))
    total = statistics.median(r["first_sound_s"] for r in rows)
    print(f"median time to first sound: {total:.2f} s (llm {'included' if args.llm_url else 'left out'})")


if __name__ == "__main__":
    main()
