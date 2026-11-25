#!/usr/bin/env bash
# Part B of topcoat_eval.py (the second-look reviewer) for several models of one node, from the
# hub host. Only /v1 is used: no chat, no note, nothing is written on the hub.
#
#   bench-review.sh <node> <outdir> <model> [<model> ...]
#
# Environment: TOPCOAT_ENV_FILE (default ../deploy/hub.env), TOPCOAT_HUB_URL (default
# https://127.0.0.1:8443), WAIT_FOR (a process pattern to wait for first).
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${TOPCOAT_ENV_FILE:-$here/../deploy/hub.env}"
hub="${TOPCOAT_HUB_URL:-https://127.0.0.1:8443}"
node="$1"; out="$2"; shift 2
mkdir -p "$out"
TOPCOAT_EVAL_KEY="$(grep -E '^TOPCOAT_DEV_ADMIN_TOKEN=' "$env_file" | head -1 | cut -d= -f2- | tr -d "'\"")"
export TOPCOAT_EVAL_KEY
while [ -n "${WAIT_FOR:-}" ] && pgrep -f "$WAIT_FOR" > /dev/null; do sleep 10; done
unload_all() { # every model that is loaded on the node: two big models in memory spoil the numbers
  local nid models m
  nid="$(python3 "$here/hubapi.py" GET /api/nodes --jq ".[] | select(.name==\"$node\") | .id" | tr -d '"')"
  models="$(python3 "$here/hubapi.py" GET /api/nodes --jq ".[] | select(.name==\"$node\") | .loaded[]?.name" | tr -d '"')"
  for m in $models; do
    printf '{"name":"models.unload","args":{"model":"%s"}}' "$m" > "$out/.unload.json"
    python3 "$here/hubapi.py" POST "/api/nodes/$nid/actions" "$out/.unload.json" > /dev/null
  done
  rm -f "$out/.unload.json"
}
for m in "$@"; do
  safe="$(printf '%s' "$m" | tr -c 'A-Za-z0-9._-' '_')"
  unload_all
  t0=$SECONDS
  python3 "$here/topcoat_eval.py" run --hub "$hub" --key-env TOPCOAT_EVAL_KEY --parts B --allow-admin --insecure \
    --review-model "$m@$node" --review-cases "$here/review_cases.json" --pause 0 \
    --label "second look $m" --out "$out/$safe.json" > "$out/$safe.log" 2>&1
  echo "$(date +%H:%M:%S) $m@$node rc=$? $((SECONDS - t0))s $(grep -E 'false_error|hit_rate|pass' "$out/$safe.log" | tr '\n' ' ' | cut -c1-200)" >> "$out/SUMMARY.txt"
  sleep 3
done
unload_all
echo "done $(date +%H:%M:%S)" >> "$out/SUMMARY.txt"
