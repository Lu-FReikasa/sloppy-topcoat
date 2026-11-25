#!/usr/bin/env bash
# Run scripts/dev/agent_bench.py against a list of models of one node, one after the other, from the
# hub host (the admin token is read from hub.env here and goes into the environment of the child
# only). Results: one JSON per model in the output directory, a one-line summary in SUMMARY.txt.
#
#   bench-models.sh <node> <outdir> <model> [<model> ...]
#
# Environment: TOPCOAT_ENV_FILE (default: ../deploy/hub.env), TOPCOAT_HUB_URL (default
# https://127.0.0.1:8443), BENCH_ARGS (extra arguments for agent_bench.py, e.g. "--repeat 2").
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${TOPCOAT_ENV_FILE:-$here/../deploy/hub.env}"
hub="${TOPCOAT_HUB_URL:-https://127.0.0.1:8443}"
node="$1"; out="$2"; shift 2
mkdir -p "$out"
TOPCOAT_API_KEY="$(grep -E '^TOPCOAT_DEV_ADMIN_TOKEN=' "$env_file" | head -1 | cut -d= -f2- | tr -d "'\"")"
export TOPCOAT_API_KEY
for m in "$@"; do
  safe="$(printf '%s' "$m" | tr -c 'A-Za-z0-9._-' '_')"
  t0=$SECONDS
  # shellcheck disable=SC2086
  python3 "$here/agent_bench.py" --base-url "$hub/v1" --model "$m@$node" --api-key-env TOPCOAT_API_KEY \
    --insecure --out "$out/$safe.json" ${BENCH_ARGS:-} > "$out/$safe.log" 2>&1
  rc=$?
  echo "$(date +%H:%M:%S) $m@$node rc=$rc $((SECONDS - t0))s $(tail -n 3 "$out/$safe.log" | tr '\n' ' ' | cut -c1-200)" >> "$out/SUMMARY.txt"
  sleep 5
done
echo "done $(date +%H:%M:%S)" >> "$out/SUMMARY.txt"
