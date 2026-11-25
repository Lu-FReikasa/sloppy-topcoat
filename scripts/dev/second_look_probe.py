"""Control probe for the second look at project results (project_check.py): does the reviewer
refuse rows the cited read does not support, and keep the ones it does?

The question of 2026-11-21: 76 of 88 second looks said "ok"; without rows known to be false that
number says nothing. This script sends the reviewer's own prompt (SYSTEM and row_lines of
project_check) with three true and three false rows about one real excerpt to each named model
and counts. Runs inside the hub container, against the hub's own /v1 with the admin token of its
environment (nothing leaves the hub):

    docker cp scripts/dev/second_look_probe.py topcoat-hub-1:/tmp/second_look_probe.py
    docker exec topcoat-hub-1 /app/hub/.venv/bin/python /tmp/second_look_probe.py \
        Qwen3.5-9B-Q4_K_M@node-tower gemma-4-26B-A4B-it-UD-Q2_K_XL@node-desk
"""

import json
import os
import ssl
import sys
import time
import urllib.request

from topcoat_hub.project_check import SYSTEM, parse_verdicts, row_lines

EXCERPT = '''web/src/project/ProjectPage.tsx
95:  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
108:  const [found, setFound] = useState<ProjectNode[] | null>(null);
255:  // a filter or a sort rule is answered by the hub (its query language), the plain tree is not
256:  useEffect(() => {
257:    if (!isProject || (!filter && !sort)) return;
260:      .searchProjectNodes({
264:        limit: 200,
269:          setFound(r.nodes);
416:  const nodes = useMemo(() => detail?.nodes ?? [], [detail]);
451:    const timer = window.setInterval(() => void loadDetail(selected), 4000);'''

# (row, true?) - the false ones are the kinds of error the reviewers are there for: a wrong number,
# a wrong purpose read into the code, a claim the excerpt does not hold at all
ROWS: list[tuple[dict[str, str], bool]] = [
    ({"Ergebnis": "collapsed startet als leeres Set (Zeile 95)", "Beleg": "E1 Z.95"}, True),
    ({"Ergebnis": "Die Suche fragt höchstens 200 Knoten ab", "Beleg": "E1 Z.264"}, True),
    ({"Ergebnis": "Die Ansicht lädt die Knoten alle 4 Sekunden neu", "Beleg": "E1 Z.451"}, True),
    ({"Ergebnis": "Die Suche fragt höchstens 500 Knoten ab", "Beleg": "E1 Z.264"}, False),
    ({"Ergebnis": "setFound lädt die Knoten beim Öffnen eines Projekts", "Beleg": "E1 Z.269"}, False),
    ({"Ergebnis": "collapsed wird im localStorage gespeichert", "Beleg": "E1 Z.95"}, False),
]


def ask(model: str, content: str) -> tuple[str, float]:
    token = os.environ["TOPCOAT_DEV_ADMIN_TOKEN"]
    payload = {
        "model": model,
        "stream": False,
        "temperature": 0,
        "max_tokens": 3000,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}],
    }
    req = urllib.request.Request(
        "https://127.0.0.1:8443/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # the hub's own CA, loopback only
    started = time.monotonic()
    with urllib.request.urlopen(req, context=ctx, timeout=600) as resp:  # noqa: S310
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"] or "", time.monotonic() - started


def main(models: list[str]) -> None:
    table = [row for row, _ in ROWS]
    content = (
        "Task:\nWie verwaltet die Projektseite das Einklappen der Baum-Ansicht?\n\n"
        + row_lines(table, {"E1": EXCERPT})
    )
    for model in models:
        try:
            answer, seconds = ask(model, content)
        except Exception as exc:  # one model that cannot answer does not stop the others
            print(f"{model}: no answer ({exc})")
            continue
        verdicts = parse_verdicts(answer, len(table))
        if verdicts is None:
            print(f"{model}: no verdict per row ({seconds:.0f} s); began {answer[:200]!r}")
            continue
        right = sum(verdicts[n] == truth for n, (_, truth) in enumerate(ROWS, 1))
        caught = sum(not verdicts[n] for n, (_, truth) in enumerate(ROWS, 1) if not truth)
        kept = sum(verdicts[n] for n, (_, truth) in enumerate(ROWS, 1) if truth)
        print(
            f"{model}: {right}/6 right - false rows refused {caught}/3, true rows kept {kept}/3"
            f" ({seconds:.0f} s)"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
