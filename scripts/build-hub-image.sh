#!/usr/bin/env bash
# Build the hub image from the current commit, tagged with that commit (the health endpoint
# reports the same commit as "build"; see hub/src/topcoat_hub/build_info.py).
#   scripts/build-hub-image.sh [image name]        (run from the repository root)
# Refuses a dirty working tree: the image must match a commit. Nothing is pushed.
set -euo pipefail
image="${1:-sloppy-topcoat-hub}"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "the working tree has uncommitted changes; commit them first" >&2
  exit 1
fi
commit="$(git rev-parse --short=7 HEAD)"
docker build -f hub/Dockerfile \
  --build-arg TOPCOAT_BUILD="${commit}" \
  -t "${image}:local-${commit}" .
echo "built ${image}:local-${commit}"
echo "check after the start: curl -sk https://<hub>:8443/api/health  (the field build = ${commit})"
