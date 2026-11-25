#!/usr/bin/env bash
# param_sweep.py for one model after the other of one node; the node's loaded models are unloaded before
# each model. Results: <outdir>/<model>.json, a line per model in <outdir>/SUMMARY.txt. Run it on the hub host.
#
#   run-sweep.sh <node> <plan.json> <outdir> <model> [<model> ...]
# The plan's own "models" list is replaced by the model@node given here (one model per run).
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${TOPCOAT_ENV_FILE:-$here/../deploy/hub.env}"
hub="${TOPCOAT_HUB_URL:-https://127.0.0.1:8443}"
node="$1"; plan="$2"; out="$3"; shift 3
mkdir -p "$out"
TOPCOAT_API_KEY="$(grep -E '^TOPCOAT_DEV_ADMIN_TOKEN=' "$env_file" | head -1 | cut -d= -f2- | tr -d "'\"")"
export TOPCOAT_API_KEY
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
  one="$out/.plan-$safe.json"
  python3 - "$plan" "$m@$node" "$one" <<'PY'
import json, sys
src, model, dst = sys.argv[1:]
plan = json.load(open(src, encoding="utf-8"))
plan["models"] = [model]
json.dump(plan, open(dst, "w", encoding="utf-8"))
PY
  cp "$(dirname "$plan")/$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['cases'])" "$plan")" "$out/" 2>/dev/null
  unload_all
  python3 "$here/param_sweep.py" run --base-url "$hub/v1" --insecure --plan "$one" --out "$out/$safe.json" > "$out/$safe.log" 2>&1
  echo "$(date +%H:%M:%S) $m rc=$?" >> "$out/SUMMARY.txt"
done
unload_all
echo "done $(date +%H:%M:%S)" >> "$out/SUMMARY.txt"
