#!/usr/bin/env python3
"""Download every pinned llama.cpp artifact and check its SHA-256.

Usage: verify-llamacpp-pins.py [path/to/releases.json]

Prints one line per asset: OK/MISMATCH, name, size, actual sha256. Exits 1 on any
mismatch or download error. Also lists the top-level entries of each archive so the
layout the node expects (llama-server / llama-bench next to the backend libraries)
can be checked in the CI log.
"""

import hashlib
import json
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "internal/llamacpp/releases.json"


def main() -> int:
    manifest = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT).read_text())
    base = manifest["base_url"].rstrip("/")
    failed = False
    with tempfile.TemporaryDirectory() as tmp:
        for tag, rel in manifest["releases"].items():
            for build in rel["builds"]:
                for asset in build["assets"]:
                    url = f"{base}/{tag}/{asset['name']}"
                    dest = Path(tmp) / asset["name"]
                    try:
                        urllib.request.urlretrieve(url, dest)  # noqa: S310 - fixed https URL
                    except Exception as exc:  # noqa: BLE001
                        print(f"ERROR    {asset['name']}: {exc}", flush=True)
                        failed = True
                        continue
                    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
                    ok = digest == asset["sha256"]
                    failed |= not ok
                    status = "OK      " if ok else "MISMATCH"
                    size = dest.stat().st_size
                    print(f"{status} {asset['name']} size={size} sha256={digest}", flush=True)
                    print("         contains:", ", ".join(interesting(dest)), flush=True)
                    dest.unlink()
    return 1 if failed else 0


def interesting(path: Path) -> list[str]:
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    else:
        with tarfile.open(path) as t:
            names = t.getnames()
    keep = ("llama-server", "llama-bench", "ggml", "cuda", "cublas", "vulkan")
    hits = [n for n in names if any(k in n.lower() for k in keep)]
    return hits[:25] or names[:10]


if __name__ == "__main__":
    sys.exit(main())
