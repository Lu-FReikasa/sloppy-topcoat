#!/usr/bin/env bash
# The roll-out of a fix onto the hub's overlay (ARCHITECTURE D104), run on the hub host by systemd
# (topcoat-fix-overlay.path watches the spool), never by the hub itself. The hub only writes a
# request into the spool: request/<id>/{manifest.json, fix.patch, READY}. This script, for each
# request:
#
#   1. checks the patch: only files of topcoat_hub/, migrations/, proto/ and web-src/src/ (the
#      GUI's source, since 2026-11-20), no "..", no absolute path, no rename or mode change;
#   2. checks the base: the overlay that runs must be the commit the fix was made on (its BUILD
#      starts with that commit; fixes rolled out before stack on top);
#   3. applies the patch to a copy of the overlay (git apply --check first, nothing is touched when
#      it does not fit);
#   4. a patch of the GUI: typecheck and build of web-src in a throw-away node container (its
#      package files are not part of any patch), the new build replaces web-dist of the copy;
#      nothing is swapped when it fails;
#   5. dumps the database when the patch changes migrations;
#   6. swaps the copy in, recreates the hub with the new build name and waits for its health;
#   7. goes back to the overlay before (and recreates again) when the hub does not answer;
#   8. writes done/<id>.json (state live | rolled_back | refused, build, message, the log's end).
#
# Installed once (deploy/fix-overlay/install.sh): this file as /usr/local/sbin/topcoat-fix-overlay
# (root-owned), the path and service units, the spool directory. Configuration: TOPCOAT_DIR (the
# directory holding deploy/ and overlay/, default /home/administrator/topcoat), TOPCOAT_FIX_SPOOL
# (default $TOPCOAT_DIR/hub-spool), TOPCOAT_HEALTH_WAIT (seconds, default 120), TOPCOAT_NODE_IMAGE
# (the image the GUI is built in, default node:22-slim).
set -euo pipefail
umask 022

dir="${TOPCOAT_DIR:-/home/administrator/topcoat}"
spool="${TOPCOAT_FIX_SPOOL:-$dir/hub-spool}"
wait_s="${TOPCOAT_HEALTH_WAIT:-120}"
node_image="${TOPCOAT_NODE_IMAGE:-node:22-slim}"
compose=(docker compose -f docker-compose.yml -f docker-compose.overlay.yml --env-file hub.env)
log_file="$(mktemp)"
trap 'rm -f "$log_file"' EXIT

say() { echo "$(date -u +%H:%M:%S) $*" | tee -a "$log_file" >&2; }

healthy() {
  local waited=0
  while [ "$waited" -lt "$wait_s" ]; do
    if curl -ksf -m 4 -o /dev/null https://127.0.0.1:8443/api/health; then return 0; fi
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

recreate() { # recreate <build>
  (cd "$dir/deploy" && TOPCOAT_OVERLAY_BUILD="$1" "${compose[@]}" up -d --force-recreate hub) >>"$log_file" 2>&1
}

finish() { # finish <id> <state> <build> <message>
  local id="$1" state="$2" build="$3" message="$4"
  mkdir -p "$spool/done"
  python3 - "$spool/done/$id.json.new" "$id" "$state" "$build" "$message" "$log_file" <<'PY'
import json, sys, datetime
out, id_, state, build, message, log = sys.argv[1:7]
tail = open(log, encoding="utf-8", errors="replace").read()[-6000:]
json.dump({"id": id_, "state": state, "build": build, "message": message, "output": tail,
           "at": datetime.datetime.now(datetime.UTC).isoformat()}, open(out, "w"))
PY
  mv -f "$spool/done/$id.json.new" "$spool/done/$id.json"
  chmod 0644 "$spool/done/$id.json"
}

check_patch() { # check_patch <file>: every path inside the overlay's parts
  python3 - "$1" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
allowed = ("topcoat_hub/", "migrations/", "proto/", "web-src/src/")
bad = []
for line in text.splitlines():
    if line.startswith(("rename ", "copy ", "old mode", "new mode", "similarity index", "GIT binary patch", "Binary files")):
        bad.append(line)
    if re.match(r"^(new file|deleted file) mode (?!100644|100755)", line):
        bad.append(line)  # a symbolic link or anything but a plain file
    m = re.match(r"^(?:---|\+\+\+) (?:a/|b/)?(\S+)", line)
    if m and m.group(1) != "/dev/null":
        p = m.group(1)
        if p.startswith("/") or ".." in p.split("/") or not p.startswith(allowed):
            bad.append(p)
    m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
    if m:
        for p in m.groups():
            if p.startswith("/") or ".." in p.split("/") or not p.startswith(allowed):
                bad.append(p)
if bad:
    print("not allowed: " + ", ".join(sorted(set(bad))[:8]))
    sys.exit(1)
PY
}

build_web() { # build_web <overlay copy>: web-src typechecked and built, web-dist replaced
  local copy="$1" out
  [ -f "$copy/web-src/package-lock.json" ] || { say "the overlay holds no web-src: deploy one first"; return 1; }
  out="$(mktemp -d)"
  mkdir -p "$dir/web-npm-cache"
  # the source read-only, built in the container's /tmp; npm fetches the packages of the lock file
  # (cached between fixes); the result is copied out
  if ! docker run --rm -v "$copy/web-src:/src:ro" -v "$dir/web-npm-cache:/npm" -v "$out:/out" \
      -e npm_config_cache=/npm "$node_image" sh -c \
      'cp -r /src /tmp/w && cd /tmp/w && npm ci --no-audit --no-fund --prefer-offline >/dev/null \
       && npm run --silent build && cp -r dist/. /out/' >>"$log_file" 2>&1; then
    rm -rf "$out"; return 1
  fi
  [ -f "$out/index.html" ] || { rm -rf "$out"; say "the build made no index.html"; return 1; }
  rm -rf "$copy/web-dist"
  mkdir -p "$copy/web-dist"
  cp -r "$out/." "$copy/web-dist/"
  rm -rf "$out"
}

one() { # one <request dir>
  local req="$1" id manifest base label migrations web cur new_build
  id="$(basename "$req")"
  [[ "$id" =~ ^[0-9a-f-]{36}$ ]] || { rm -rf "$req"; return; }
  : >"$log_file"
  manifest="$req/manifest.json"
  base="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["base_commit"])' "$manifest" 2>/dev/null || true)"
  label="$(python3 -c 'import json,re,sys;l=json.load(open(sys.argv[1]))["label"];print(l if re.fullmatch(r"[A-Za-z0-9.-]{1,40}",l) else "")' "$manifest" 2>/dev/null || true)"
  migrations="$(python3 -c 'import json,sys;print("1" if json.load(open(sys.argv[1])).get("migrations") else "0")' "$manifest" 2>/dev/null || echo 0)"
  # the patch decides, not only the manifest: a change of web-src is always built
  web=0
  grep -q '^diff --git a/web-src/' "$req/fix.patch" 2>/dev/null && web=1
  if [ -z "$label" ] || [ ! -f "$req/fix.patch" ]; then
    finish "$id" refused "" "the request is incomplete"; rm -rf "$req"; return
  fi
  if ! msg="$(check_patch "$req/fix.patch")"; then
    finish "$id" refused "" "the patch touches files outside the overlay: $msg"; rm -rf "$req"; return
  fi
  cur="$(sed -n '1s/ .*//p' "$dir/overlay/BUILD" 2>/dev/null || true)"
  if [ -z "$cur" ]; then
    finish "$id" refused "" "the hub runs no overlay: deploy one first"; rm -rf "$req"; return
  fi
  if [ -z "$base" ] || [ "${cur:0:7}" != "${base:0:7}" ]; then
    finish "$id" refused "$cur" "the hub runs $cur, the fix was made on $base: make it again on the code that runs"
    rm -rf "$req"; return
  fi
  say "fix $label on $cur"
  rm -rf "$dir/overlay.new"
  cp -a "$dir/overlay" "$dir/overlay.new"
  if ! (cd "$dir/overlay.new" && git apply --check "$req/fix.patch") >>"$log_file" 2>&1; then
    rm -rf "$dir/overlay.new"
    finish "$id" refused "$cur" "the patch does not fit the code that runs (git apply --check)"
    rm -rf "$req"; return
  fi
  (cd "$dir/overlay.new" && git apply "$req/fix.patch") >>"$log_file" 2>&1
  if [ "$web" = 1 ]; then
    say "the GUI changes: typecheck and build"
    if ! build_web "$dir/overlay.new"; then
      rm -rf "$dir/overlay.new"
      finish "$id" refused "$cur" "the GUI did not build with the fix (typecheck or build failed, see the output)"
      rm -rf "$req"; return
    fi
  fi
  new_build="${cur}+${label}"
  sums="$(sed -n '1s/^[^ ]* //p' "$dir/overlay/BUILD")"
  echo "$new_build $sums" >"$dir/overlay.new/BUILD"
  # this runs as root (the request files belong to the hub's user): the overlay stays the deploy
  # user's, so the next deploy from the maintainer's checkout can replace it
  chown -R "$(stat -c %U:%G "$dir/overlay")" "$dir/overlay.new"
  chmod -R a+rX "$dir/overlay.new"
  if [ "$migrations" = 1 ]; then
    say "migrations change: dump first"
    mkdir -p "$dir/backup"
    (cd "$dir/deploy" && "${compose[@]}" exec -T db pg_dump -U topcoat -Fc topcoat) >"$dir/backup/topcoat-before-$new_build.dump"
  fi
  rm -rf "$dir/overlay.old"
  mv "$dir/overlay" "$dir/overlay.old"
  mv "$dir/overlay.new" "$dir/overlay"
  recreate "$new_build"
  if healthy; then
    say "live: $new_build"
    finish "$id" live "$new_build" "the hub runs $new_build"
  else
    say "the hub did not answer with $new_build: back to $cur"
    (cd "$dir/deploy" && "${compose[@]}" logs --tail 40 hub) >>"$log_file" 2>&1 || true
    rm -rf "$dir/overlay.failed"
    mv "$dir/overlay" "$dir/overlay.failed"
    mv "$dir/overlay.old" "$dir/overlay"
    recreate "${cur}"
    if healthy; then
      finish "$id" rolled_back "$cur" "the hub did not answer with the fix; it runs $cur again"
    else
      finish "$id" rolled_back "$cur" "the hub did not answer with the fix, nor after going back: look at it by hand"
    fi
  fi
  rm -rf "$req"
}

exec 9>"$spool/.lock"
flock -n 9 || exit 0
for req in "$spool"/request/*/; do
  [ -f "$req/READY" ] || continue
  one "${req%/}"
done
