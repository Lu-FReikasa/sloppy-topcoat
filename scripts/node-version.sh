#!/usr/bin/env bash
# The version string of a topcoat-node build, always MAJOR.MINOR.PATCH[-pre] (the form the update
# channel accepts, node/internal/update). One source: the VERSION file at the root of the repository.
#
#   scripts/node-version.sh             0.2.0-dev.1a2b3c4  (a build from a working copy: older than 0.2.0)
#   scripts/node-version.sh --release   0.2.0              (what is signed and published)
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
base="$(tr -d '[:space:]' < "$root/VERSION")"
if [ "${1:-}" = "--release" ]; then
  printf '%s\n' "$base"
  exit 0
fi
sha="$(git -C "$root" rev-parse --short=7 HEAD 2>/dev/null || echo unknown)"
dirty=""
if [ -n "$(git -C "$root" status --porcelain --untracked-files=no 2>/dev/null)" ]; then dirty=".dirty"; fi
printf '%s-dev.%s%s\n' "$base" "$sha" "$dirty"
