#!/usr/bin/env bash
# Overlay deploy: put the hub's Python code (and optionally the built GUI) from this checkout
# onto the hub host without building an image. The files are copied into a directory next to the
# compose file and bind-mounted over the image's copy; only the hub container is recreated, which
# takes seconds. Meant for tight development loops on a hobby hub, not as the release path:
# a clean image (scripts/build-hub-image.sh + deploy/update-hub.sh) ends every session, and a
# plain `update-hub.sh` run removes the overlay again because it uses the base compose file only.
#
#   scripts/dev/deploy-overlay.sh push [--web]   copy hub/src, hub/migrations, proto (and web/dist), restart the hub
#   scripts/dev/deploy-overlay.sh status         what runs: image, overlay commit, health
#   scripts/dev/deploy-overlay.sh off            back to the code inside the image
#
# Environment:
#   TOPCOAT_OVERLAY_HOST   ssh alias of the hub host (default: hub.example.lan)
#   TOPCOAT_OVERLAY_DIR    directory on the host that holds deploy/ (default: topcoat, relative to $HOME)
#   TOPCOAT_HEALTH_WAIT    seconds to wait for the health URL (default: 90)
#
# Safety: a pg_dump goes to <dir>/backup before the first start with changed migrations, and a hub
# that does not answer after a push is switched back to the image code by the script itself.
# Uncommitted changes are refused so that the shown build always names a real commit.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
[ -f "$root/.dev/hosts.env" ] && . "$root/.dev/hosts.env"
host="${TOPCOAT_OVERLAY_HOST:-hub.example.lan}"
rdir="${TOPCOAT_OVERLAY_DIR:-topcoat}"
wait_s="${TOPCOAT_HEALTH_WAIT:-90}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

remote() { ssh -o BatchMode=yes "$host" "$@"; }

# The remote part is one script text on stdin, so quoting stays in one place.
compose_cmd='cd "$HOME/'"$rdir"'/deploy" && docker compose -f docker-compose.yml'

write_override() { # write_override <with_web 0|1>
  local web="$1" spool=0
  # the roll-out of issue fixes (D104, deploy/fix-overlay/install.sh): the hub queues into it
  remote "[ -d \$HOME/$rdir/hub-spool/request ]" && spool=1
  remote "cat > \$HOME/$rdir/deploy/docker-compose.overlay.yml" <<EOF
# Written by scripts/dev/deploy-overlay.sh. Remove this file (or run "off") to use the image code.
services:
  hub:
    environment:
      TOPCOAT_BUILD: \${TOPCOAT_OVERLAY_BUILD:-overlay}
$( [ "$spool" = 1 ] && echo "      TOPCOAT_FIX_SPOOL_DIR: /spool" )
    volumes:
      - ../overlay/topcoat_hub:/app/hub/src/topcoat_hub:ro
      - ../overlay/migrations:/app/hub/migrations:ro
      - ../overlay/proto:/app/proto:ro
$( [ "$web" = 1 ] && echo "      - ../overlay/web-dist:/app/web/dist:ro" )
$( [ "$spool" = 1 ] && echo "      - ../hub-spool:/spool" )
EOF
}

# Fixes the hub rolled out itself (issue_fixes.py, D104) live only on the hub until they are in this
# checkout: a push from here would drop them. scripts/dev/pull-fixes.py brings them in as commits
# with the trailer "Topcoat-Fix: <label>".
check_fixes() {
  local build labels missing=""
  build="$( { remote "cat \$HOME/$rdir/overlay/BUILD 2>/dev/null" || true; } | sed -n '1s/ .*//p')"
  labels="$(echo "$build" | tr '+' '\n' | sed -n 's/^\(fix-[A-Za-z0-9.-]*\)$/\1/p')"
  for label in $labels; do
    # git searches itself: `git log | grep -q` fails under pipefail once grep stops reading early
    [ -n "$(git -C "$root" log -1 --format=%h --grep="^Topcoat-Fix: $label\$")" ] \
      || missing="$missing $label"
  done
  if [ -n "$missing" ] && [ "${TC_DROP_FIXES:-0}" != 1 ]; then
    echo "the hub runs fixes this checkout does not have:$missing" >&2
    echo "bring them in first: python scripts/dev/pull-fixes.py (or TC_DROP_FIXES=1 to drop them)" >&2
    exit 1
  fi
}

wait_healthy() {
  local waited=0
  while [ "$waited" -lt "$wait_s" ]; do
    if remote 'curl -ksf -m 4 -o /dev/null https://127.0.0.1:8443/api/health'; then return 0; fi
    sleep 3; waited=$((waited + 3))
  done
  return 1
}

cmd_off() {
  remote "$compose_cmd --env-file hub.env up -d hub" >/dev/null
  remote "rm -f \$HOME/$rdir/deploy/docker-compose.overlay.yml"
  wait_healthy && echo "overlay off, the hub runs the image code" || { echo "hub does not answer after 'off'" >&2; exit 1; }
}

cmd_status() {
  remote "$compose_cmd --env-file hub.env ps hub; ls \$HOME/$rdir/deploy/docker-compose.overlay.yml 2>/dev/null && echo 'overlay: ON' || echo 'overlay: off'; cat \$HOME/$rdir/overlay/BUILD 2>/dev/null || true"
  remote 'curl -ksf -m 4 https://127.0.0.1:8443/api/health' || echo "health: no answer"
  echo
}

cmd_push() {
  local web=0
  [ "${1:-}" = "--web" ] && web=1
  cd "$root"
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "uncommitted changes: commit first, the overlay build must name a commit" >&2; exit 1
  fi
  check_fixes
  local build; build="$(git rev-parse --short HEAD)-overlay"
  local mig_sum; mig_sum="$(git ls-files hub/migrations | sort | xargs -I{} git hash-object {} | md5sum | cut -c1-12)"

  if [ "$web" = 1 ] && [ "${TC_WEB_PREBUILT:-0}" = 1 ]; then
    # the dist was built elsewhere (a Linux copy when web/node_modules comes from Windows): use it as is
    [ -f web/dist/index.html ] || { echo "TC_WEB_PREBUILT=1 but web/dist/index.html is missing" >&2; exit 1; }
  elif [ "$web" = 1 ]; then
    # web/dist is not in git and goes stale: build it from this checkout every time
    [ -d web/node_modules ] || { echo "web/node_modules missing: run 'npm ci' in web/ first" >&2; exit 1; }
    (cd web && npm run build >/dev/null) || { echo "the web build failed" >&2; exit 1; }
  fi

  # 1. copy into a fresh staging directory next to the live overlay, then swap
  remote "rm -rf \$HOME/$rdir/overlay.new && mkdir -p \$HOME/$rdir/overlay.new/topcoat_hub \$HOME/$rdir/overlay.new/migrations \$HOME/$rdir/overlay.new/proto \$HOME/$rdir/overlay.new/web-dist"
  tar -C hub/src/topcoat_hub --exclude=__pycache__ -cf - . | remote "tar -C \$HOME/$rdir/overlay.new/topcoat_hub -xf -"
  tar -C hub/migrations --exclude=__pycache__ -cf - . | remote "tar -C \$HOME/$rdir/overlay.new/migrations -xf -"
  tar -C proto -cf - . | remote "tar -C \$HOME/$rdir/overlay.new/proto -xf -"
  if [ "$web" = 1 ]; then tar -C web/dist -cf - . | remote "tar -C \$HOME/$rdir/overlay.new/web-dist -xf -"; fi
  # the GUI's source of this commit (tracked files only): a fix of web/src is built from it on the host
  # (deploy/fix-overlay/hub-fix-overlay.sh)
  remote "mkdir -p \$HOME/$rdir/overlay.new/web-src"
  git archive HEAD web | remote "tar -C \$HOME/$rdir/overlay.new/web-src --strip-components=1 -xf -"
  remote "echo '$build migrations=$mig_sum' > \$HOME/$rdir/overlay.new/BUILD; chmod -R a+rX \$HOME/$rdir/overlay.new"

  # 2. a dump when the migrations are not the ones that ran before
  local old_sum; old_sum="$( { remote "cat \$HOME/$rdir/overlay/BUILD 2>/dev/null" || true; } | sed -n 's/.*migrations=//p')"
  if [ "$old_sum" != "$mig_sum" ]; then
    echo "migrations differ from the last overlay (or first overlay): dump first"
    remote "mkdir -p \$HOME/$rdir/backup && $compose_cmd --env-file hub.env exec -T db pg_dump -U topcoat -Fc topcoat > \$HOME/$rdir/backup/topcoat-before-$build.dump"
  fi

  # a push without --web keeps the GUI of the last overlay: a fresh empty web-dist would drop the
  # mount and the hub would fall back to the (older) GUI inside the image
  if [ "$web" = 0 ] && remote "[ -n \"\$(ls -A \$HOME/$rdir/overlay/web-dist 2>/dev/null)\" ]"; then
    remote "cp -a \$HOME/$rdir/overlay/web-dist/. \$HOME/$rdir/overlay.new/web-dist/"
    web=1
  fi

  # 3. swap, write the override, recreate the hub
  remote "cd \$HOME/$rdir && rm -rf overlay.old && { [ -d overlay ] && mv overlay overlay.old || true; } && mv overlay.new overlay"
  write_override "$web"
  remote "export TOPCOAT_OVERLAY_BUILD='$build'; $compose_cmd -f docker-compose.overlay.yml --env-file hub.env up -d --force-recreate hub" >/dev/null
  if wait_healthy; then
    echo "overlay $build is live"
    # the assistant reads the code the hub runs: the same commit goes into the dev node's workshop
    "$root/scripts/dev/push-live-to-devnode.sh" || echo "the live code did not reach the dev node (see above)" >&2
  else
    echo "the hub did not answer within ${wait_s}s with $build - switching back to the image code" >&2
    remote "$compose_cmd -f docker-compose.overlay.yml --env-file hub.env logs --tail 30 hub" >&2 || true
    cmd_off || true
    exit 1
  fi
}

case "${1:-}" in
  push) shift; cmd_push "$@" ;;
  status) cmd_status ;;
  off) cmd_off ;;
  *) sed -n '2,17p' "$0"; exit 2 ;;
esac
