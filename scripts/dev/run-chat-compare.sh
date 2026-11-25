#!/usr/bin/env bash
# chat_compare.py for several models of one node, one after the other; the node's loaded models are
# unloaded before each one. Answers: <outdir>/<model>.json.
#
#   run-chat-compare.sh <node> <outdir> <model> [<model> ...]      (WAIT_FOR: a process pattern to wait for)
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${TOPCOAT_ENV_FILE:-$here/../deploy/hub.env}"
hub="${TOPCOAT_HUB_URL:-https://127.0.0.1:8443}"
node="$1"; out="$2"; shift 2
mkdir -p "$out"
TOPCOAT_API_KEY="$(grep -E '^TOPCOAT_DEV_ADMIN_TOKEN=' "$env_file" | head -1 | cut -d= -f2- | tr -d "'\"")"
export TOPCOAT_API_KEY
while [ -n "${WAIT_FOR:-}" ] && pgrep -f "$WAIT_FOR" > /dev/null; do sleep 10; done
unload_all() {
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
  python3 "$here/chat_compare.py" --base-url "$hub/v1" --insecure --out "$out/$safe.json" "$m@$node" > "$out/$safe.log" 2>&1
  echo "$(date +%H:%M:%S) $m rc=$?" >> "$out/SUMMARY.txt"
done
unload_all
echo "done $(date +%H:%M:%S)" >> "$out/SUMMARY.txt"
