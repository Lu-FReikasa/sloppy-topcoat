#!/usr/bin/env python3
"""Generate the knowledge base's directory ``10_KI_Verzeichnis.md`` (E3-04): for every host and
service, which sections of which notes are about it, with line numbers. The hub's knowledge search
looks a host or service named in a question up there first and returns exactly those sections.

    python scripts/dev/make_kb_index.py --root /path/to/notes               # entities = file titles
    python scripts/dev/make_kb_index.py --root /path/to/notes --names names.txt

``names.txt`` lists the hosts and services (one per line, ``name: alias, alias``, ``#`` comments);
every section whose heading, or whose first lines, mention a name or alias as a whole word becomes
a reference of that entry. Without ``--names`` every note is an entity named by its first ``#``
heading (and its file name), and its ``##`` sections are the references.

Nothing is read that the knowledge base would not read: files with an excluded name
(``*SENSIBEL*``, ``*password*``, ``.env`` ...), other extensions than .md/.markdown/.txt and files
over 1 MiB are skipped (the same rules as the hub). The file is written atomically, so a cron job
can run this at any time; ``--check`` writes nothing and exits 1 when the file would change.
Standard library only; it imports the hub's own parser (the format cannot drift)."""

import argparse
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hub" / "src"))
from topcoat_hub import knowledge_directory as kd  # noqa: E402
from topcoat_hub import knowledge_rules as rules  # noqa: E402

PEEK_LINES = 3  # lines under a heading that count as "the section is about it"


def notes(root: Path, output_name: str) -> dict[str, str]:
    """``relative path -> text`` of every note the knowledge base would read."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not rules.excluded(d))
        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            if name.lower() == output_name.lower() or not rules.wanted(rel):
                continue
            if rules.excluded(rel):
                continue
            path = root / rel
            if path.stat().st_size > rules.MAX_FILE_BYTES:
                continue
            out[rel] = path.read_text(encoding="utf-8", errors="replace")
    return out


def read_names(path: Path) -> list[tuple[str, list[str]]]:
    names: list[tuple[str, list[str]]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not line:
            continue
        name, _, aliases = line.partition(":")
        names.append(
            (name.strip(), [a.strip() for a in aliases.split(",") if a.strip()])
        )
    return names


def by_names(
    files: dict[str, str], names: list[tuple[str, list[str]]]
) -> list[kd.Entry]:
    entries: list[kd.Entry] = []
    for name, aliases in names:
        pats = [
            re.compile(rf"(?<![\w\-]){re.escape(n)}(?![\w\-])", re.IGNORECASE)
            for n in [name, *aliases]
        ]
        refs: list[kd.Ref] = []
        for rel, text in files.items():
            lines = text.splitlines()
            for _level, heading, start, end in kd.outline(text):
                peek = " ".join(lines[start : min(start + PEEK_LINES, end)])
                if any(p.search(heading) or p.search(peek) for p in pats):
                    refs.append(kd.Ref(rel, start, end, heading))
        if refs:
            entries.append(kd.Entry(name, aliases, refs))
    return entries


def by_titles(files: dict[str, str]) -> list[kd.Entry]:
    entries: list[kd.Entry] = []
    for rel, text in files.items():
        out = kd.outline(text)
        if not out:
            continue
        title = next((h for lvl, h, _s, _e in out if lvl == 1), None) or Path(rel).stem
        stem = Path(rel).stem
        refs = [kd.Ref(rel, s, e, h) for lvl, h, s, e in out if lvl == 2]
        if not refs:
            refs = [kd.Ref(rel, out[0][2], out[0][3], out[0][1])]
        entries.append(
            kd.Entry(title, [stem] if stem.lower() != title.lower() else [], refs)
        )
    return entries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", required=True, type=Path, help="the folder of the notes")
    ap.add_argument(
        "--names",
        type=Path,
        help="hosts and services, one per line (name: alias, alias)",
    )
    ap.add_argument("--output", type=Path, help=f"default: <root>/{kd.DIRECTORY_FILE}")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    ap.add_argument(
        "--check",
        action="store_true",
        help="write nothing, exit 1 if the file would change",
    )
    args = ap.parse_args(argv)
    if not args.root.is_dir():
        ap.error(f"{args.root} is not a folder")
    output = args.output or args.root / kd.DIRECTORY_FILE
    files = notes(args.root, kd.DIRECTORY_FILE)
    entries = (
        by_names(files, read_names(args.names)) if args.names else by_titles(files)
    )
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = kd.render(entries, stamp, args.root.name)
    if args.stdout:
        sys.stdout.write(text)
        return 0
    stable = re.sub(r"on \d{4}-\d\d-\d\dT[\d:]+Z", "on -", text)
    old = output.read_text(encoding="utf-8") if output.exists() else ""
    if stable == re.sub(r"on \d{4}-\d\d-\d\dT[\d:]+Z", "on -", old):
        print(f"{output}: up to date ({len(entries)} entries)")
        return 0
    if args.check:
        print(f"{output}: would change ({len(entries)} entries)")
        return 1
    fd, tmp = tempfile.mkstemp(dir=output.parent, prefix=".kb-index-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, output)
    print(
        f"{output}: {len(entries)} entries, {sum(len(e.refs) for e in entries)} sections"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
