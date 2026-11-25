"""Before/after measurement of the prompt-cache layout (E2-05 A4, D27): the hub's real prompt
builders (topcoat_hub.chat) against a real llama-server. With a tiny random model the numbers
show how many prompt tokens the server had to process again for each question, not real answer
speed (run it against a real model for that).

    LLAMA_DIR=/path/to/llama-b11223 TINY_GGUF=tiny.gguf python scripts/dev/measure_prompt_cache.py
"""
import json, os, signal, subprocess, sys, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hub", "src"))
from topcoat_hub import chat

BIN = os.environ["LLAMA_DIR"]  # the unpacked llama.cpp release (CPU build is enough)
env = dict(os.environ, LD_LIBRARY_PATH=BIN)
PORT = 18111

def start():
    p = subprocess.Popen([f"{BIN}/llama-server", "-m", os.environ["TINY_GGUF"], "--host", "127.0.0.1",
                          "--port", str(PORT), "-c", "8192", "-np", "1", "--device", "none", "--no-webui", "-lv", "0"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
            return p
        except Exception:
            time.sleep(0.2)
    raise SystemExit("server did not start")

def ask(messages):
    body = json.dumps({"model": "x", "messages": messages, "max_tokens": 4, "cache_prompt": True, "temperature": 0}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions", body, {"Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=120).read())
    t = r["timings"]
    return t["prompt_n"], t["cache_n"], t["prompt_ms"]

caps = chat.Capabilities(memory=True)
paragraph = ("Das ist eine längere Nachricht mit einigem Inhalt, damit der Verlauf eines Gesprächs "
             "eine realistische Größe hat. ") * 3
questions = ["Wo wohne ich?", "Wie heißt mein Hund?", "Was arbeite ich?", "Welche Katze habe ich?", "Was trinke ich gern?", "Wo arbeite ich?"]
notes = [[("doc", "active", "Wohnt in Hamburg", 0)],
         [("doc", "active", "Hund heißt Bello", 2), ("doc", "active", "Wohnt in Hamburg", 0)],
         [("doc", "active", "Arbeitet als Tischler", 0)],
         [("doc", "active", "Katze heißt Mimi", 1)],
         [("doc", "active", "Trinkt Espresso", 0), ("assumption", "active", "Ist müde", 0)],
         [("doc", "active", "Arbeitet als Tischler", 0), ("doc", "active", "Werkstatt in Kiel", 0)]]

def run(layout):
    history = []
    rows = []
    for q, n in zip(questions, notes):
        history.append(("user", paragraph + q))
        block = chat.notes_context([x for x in n])
        if layout == "notes in the system prompt (before)":
            system = chat.system_prompt(None, "du", caps) + "\n\n" + block.replace(chat.NOTES_FOOTER + "\n", "")
            prompt = chat.build_prompt(system, history, 100000)
        else:
            system = chat.system_prompt(None, "du", caps)
            prompt = chat.build_prompt(system, history, 100000, block)
        processed, cached, ms = ask(prompt.messages)
        rows.append((processed, cached, ms))
        history.append(("assistant", paragraph + "Antwort."))
    return rows

for layout in ("notes in the system prompt (before)", "notes in the last user turn (after)"):
    p = start()
    try:
        ask([{"role": "user", "content": "warm up"}])
        rows = run(layout)
    finally:
        p.send_signal(signal.SIGTERM); p.wait()
    print(layout)
    tot_p = sum(r[0] for r in rows); tot_ms = sum(r[2] for r in rows)
    for i, (pn, cn, ms) in enumerate(rows, 1):
        print(f"  question {i}: processed {pn:5d} prompt tokens, reused from cache {cn:5d}, {ms:7.1f} ms")
    print(f"  total: processed {tot_p} tokens, {tot_ms:.0f} ms of prompt processing")
