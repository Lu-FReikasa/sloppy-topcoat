#!/usr/bin/env python3
"""Call the hub's API from the hub host, with the admin token read from the env file.

    hubapi.py GET /api/nodes
    hubapi.py POST /api/conversations body.json
    hubapi.py GET /api/activity --jq '.[0]'      (needs jq on the host)

The token is read from hub.env (TOPCOAT_DEV_ADMIN_TOKEN) by this script and goes into one
request header: never onto a command line, into a log or into the output. Only for the loopback
address of the hub host (the certificate is the hub's own CA, so it is not verified here).

Environment: TOPCOAT_ENV_FILE (default: ../deploy/hub.env next to this script's folder),
TOPCOAT_HUB_URL (default: https://127.0.0.1:8443).
"""

import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _token() -> str:
    env_file = Path(os.environ.get("TOPCOAT_ENV_FILE", Path(__file__).resolve().parent.parent / "deploy" / "hub.env"))
    for line in env_file.read_text().splitlines():
        if line.startswith("TOPCOAT_DEV_ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("TOPCOAT_DEV_ADMIN_TOKEN not found in the env file")


def main(argv: list[str]) -> int:
    args = list(argv)
    jq = None
    if "--jq" in args:
        i = args.index("--jq")
        jq = args[i + 1]
        del args[i : i + 2]
    if len(args) < 2:
        sys.exit(__doc__)
    method, path = args[0].upper(), args[1]
    body = Path(args[2]).read_bytes() if len(args) > 2 else None
    url = os.environ.get("TOPCOAT_HUB_URL", "https://127.0.0.1:8443") + path
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Bearer " + _token())
    if body is not None:
        req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=600) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"HTTP {e.code}\n")
        data = e.read()
    text = data.decode("utf-8", "replace")
    if jq:
        out = subprocess.run(["jq", jq], input=text, capture_output=True, text=True)
        sys.stdout.write(out.stdout or out.stderr)
    else:
        try:
            text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
        except ValueError:
            pass
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
