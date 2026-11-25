#!/usr/bin/env bash
# Run changeset_bench.py for several models of one node, once per tool file (for example the
# tool definitions before and after a change), from the hub host. Before each model every model
# loaded on the node is unloaded (two big models in memory made the numbers useless), and a result
# that exists is not made again, so a stopped run can be started again.
#
#   bench-changeset.sh <node> <outdir> "<tools1.json tools2.json ...>" <model> [<model> ...]
#
# Environment: TOPCOAT_ENV_FILE (default ../deploy/hub.env), TOPCOAT_HUB_URL (default
# https://127.0.0.1:8443), REPEAT (default 3), WAIT_FOR (a process pattern to wait for first).
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${TOPCOAT_ENV_FILE:-$here/../deploy/hub.env}"
hub="${TOPCOAT_HUB_URL:-https://127.0.0.1:8443}"
node="$1"; out="$2"; tools="$3"; shift 3
mkdir -p "$out"
TOPCOAT_API_KEY="$(grep -E '^TOPCOAT_DEV_ADMIN_TOKEN=' "$env_file" | head -1 | cut -d= -f2- | tr -d "'\"")"
export TOPCOAT_API_KEY
while [ -n "${WAIT_FOR:-}" ] && pgrep -f "$WAIT_FOR" > /dev/null; do sleep 10; done

unload_all() { # every model that is loaded on the node
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
  for t in $tools; do
    tag="$(basename "$t" .json)"
    [ -s "$out/$safe.$tag.json" ] && continue
    t0=$SECONDS
    python3 "$here/changeset_bench.py" run --tools "$here/$t" --base-url "$hub/v1" --model "$m@$node" \
      --insecure --repeat "${REPEAT:-3}" --out "$out/$safe.$tag.json" > "$out/$safe.$tag.log" 2>&1
    echo "$(date +%H:%M:%S) $m@$node $tag $((SECONDS - t0))s $(tail -n 1 "$out/$safe.$tag.log" | cut -c1-200)" >> "$out/SUMMARY.txt"
  done
  sleep 3
done
unload_all
echo "done $(date +%H:%M:%S)" >> "$out/SUMMARY.txt"
