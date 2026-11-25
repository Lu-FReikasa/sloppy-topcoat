#!/usr/bin/env bash
# Roll a hub image out: backup tag -> checks -> build (or pull) -> smoke test -> way-back image ->
# transfer -> production. One step at a time, each with its own question, so nothing happens that
# was not confirmed (D52: the assistant never runs this; the maintainer does).
#
#   scripts/dev/release.sh [--dry-run] [--yes] [--stop-after <step>] [--image <ref>]
#
#   --dry-run          print what each step would do; change nothing, ask nothing
#   --yes              do not ask before a step (the checks and the build still run in order)
#   --stop-after STEP  stop cleanly after STEP: tag check build|pull smoke wayback transfer
#   --image REF        the image to roll out (default sloppy-topcoat-hub:local-<sha>). A registry
#                      image (ghcr.io/<owner>/sloppy-topcoat-hub:sha-<commit>, built by CI, D123)
#                      is pulled instead of built; any other name is built locally as before.
#
# Where things are comes from the environment, never from the repository:
#   TOPCOAT_PROD_SSH   user@host of the production Docker server        (required for the remote steps)
#   TOPCOAT_PROD_DIR   its deploy directory, e.g. ~/topcoat/deploy        (default: ~/topcoat/deploy)
#   TOPCOAT_BACKUP_REMOTE  where the way-back image is saved on prod      (default: ~/topcoat/backup)
# The remote side is deploy/update-hub.sh, which brings its own database dump, revision check,
# health check and automatic rollback; this script adds the way-back image.
set -euo pipefail

dry=0 yes=0 stop_after="" image=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=1; shift ;;
    --yes) yes=1; shift ;;
    --stop-after) stop_after="${2:?--stop-after needs a step}"; shift 2 ;;
    --image) image="${2:?--image needs a reference}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
prod_ssh="${TOPCOAT_PROD_SSH:-}"
prod_dir="${TOPCOAT_PROD_DIR:-~/topcoat/deploy}"
backup_remote="${TOPCOAT_BACKUP_REMOTE:-~/topcoat/backup}"
sha="$(git rev-parse --short=7 HEAD)"
image="${image:-sloppy-topcoat-hub:local-$sha}"
# a registry image (its first part names a host: has a dot or a port, or is localhost) is pulled,
# anything else is built here (D123: CI builds, the maintainer rolls out)
pull=0
case "$image" in
  */*) first="${image%%/*}"
       case "$first" in *.*|*:*|localhost) pull=1 ;; esac ;;
esac
# the branch the backup tag marks: origin/main, or the local main while there is no remote
main_ref=origin/main
git rev-parse -q --verify "$main_ref" >/dev/null || main_ref=main
main_sha="$(git rev-parse --short=7 "$main_ref" 2>/dev/null || echo unknown)"
tag="backup/$(date +%Y-%m-%d)-main-$main_sha"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

step_no=0
say() { printf '\n== %s\n' "$*"; }
ask() { # <question>: yes/no, unless --yes or --dry-run
  [ "$dry" -eq 1 ] && return 0
  [ "$yes" -eq 1 ] && return 0
  local a; read -r -p "$1 [y/N] " a; [ "$a" = "y" ] || [ "$a" = "Y" ]
}
run() { # a command that changes something: shown in a dry run, run otherwise
  if [ "$dry" -eq 1 ]; then printf '   (dry run) %s\n' "$*"; else "$@"; fi
}
step() { # <name> <title>: announces the step and returns 1 when it is to be skipped
  step_no=$((step_no + 1)); say "step $step_no ($1): $2"
}
finish() { [ "$stop_after" = "$1" ] && { say "stopped after $1 as asked"; exit 0; }; return 0; }
need_remote() { [ -n "$1" ] || { echo "set TOPCOAT_PROD_SSH (user@host) for the remote steps" >&2; exit 64; }; }

# 1 --------------------------------------------------------------------------------------------
step tag "backup tag $tag on $main_ref ($main_sha), so nothing is lost"
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "   the tag exists already"
elif [ "$main_sha" = unknown ] && [ "$dry" -eq 0 ]; then
  echo "   neither origin/main nor main is known here: run 'git fetch origin main' first" >&2; exit 65
elif ask "create the local tag $tag?"; then
  run git tag "$tag" "$main_ref"
  echo "   the tag is local; pushing it is your call: git push origin $tag"
fi
finish tag

# 2 --------------------------------------------------------------------------------------------
step check "make check (lint, types, tests: the same as CI)"
if [ -n "$(git status --porcelain)" ]; then echo "   the working tree is not clean - commit or stash first" >&2; [ "$dry" -eq 1 ] || exit 65; fi
# WSL on a Windows checkout: make check would rebuild hub/.venv for Linux and wreck the Windows one
if grep -qi microsoft /proc/version 2>/dev/null && [ "${root#/mnt/}" != "$root" ]; then
  echo "   WARNING: this is WSL on a Windows folder (/mnt/...): run the checks from Windows, or" >&2
  echo "   from a Linux clone, and answer N here (docs/DEVELOPMENT.md)" >&2
fi
if [ "$pull" -eq 1 ] && [[ "$image" =~ :sha-([0-9a-f]{7,})$ ]] && [[ "$(git rev-parse HEAD)" != "${BASH_REMATCH[1]}"* ]]; then
  echo "   NOTE: the checks run on this checkout ($sha), the image was built from ${BASH_REMATCH[1]}"
fi
ask "run the checks?" && run make check
finish check

# 3 --------------------------------------------------------------------------------------------
if [ "$pull" -eq 1 ]; then
  step pull "docker pull $image (built by CI)"
  ask "pull the image?" && run docker pull "$image"
  finish pull
else
  step build "docker build -> $image"
  ask "build the image?" && run docker build -f hub/Dockerfile -t "$image" .
  finish build
fi

# 4 --------------------------------------------------------------------------------------------
step smoke "smoke test of the image (migrates, serves TLS, keeps its CA)"
ask "run the smoke test?" && run scripts/smoke-test-hub-image.sh "$image"
finish smoke

# 5 --------------------------------------------------------------------------------------------
step wayback "save the image that runs in production now (the way back)"
need_remote "$prod_ssh"
if [ "$dry" -eq 1 ]; then
  echo "   (dry run) ssh $prod_ssh: read TOPCOAT_IMAGE from $prod_dir/hub.env, docker save | gzip > $backup_remote/wayback-<image>.tar.gz"
elif ask "save the running production image on the server?"; then
  ssh "$prod_ssh" "set -e; cur=\$(grep -E '^TOPCOAT_IMAGE=' $prod_dir/hub.env | head -1 | cut -d= -f2-); [ -n \"\$cur\" ]; mkdir -p $backup_remote; docker save \"\$cur\" | gzip > $backup_remote/wayback-\$(printf '%s' \"\$cur\" | tr -c 'A-Za-z0-9._-' _).tar.gz; ls -l $backup_remote | tail -3"
fi
finish wayback

# 6 --------------------------------------------------------------------------------------------
step transfer "send $image to the server (docker save | gzip | ssh)"
need_remote "$prod_ssh"
if [ "$dry" -eq 1 ]; then
  echo "   (dry run) docker save $image | gzip | ssh $prod_ssh 'cat > /tmp/topcoat-image.tar.gz'"
elif ask "transfer the image to $prod_ssh?"; then
  docker save "$image" | gzip | ssh "$prod_ssh" 'cat > /tmp/topcoat-image.tar.gz'
fi
finish transfer

# 7 --------------------------------------------------------------------------------------------
step prod "update production with deploy/update-hub.sh (dump, revision check, health check, auto-rollback)"
need_remote "$prod_ssh"
if [ "$dry" -eq 1 ]; then
  echo "   (dry run) ssh $prod_ssh 'cd $prod_dir && ./update-hub.sh --dry-run --load /tmp/topcoat-image.tar.gz $image' and then without --dry-run"
else
  ssh "$prod_ssh" "cd $prod_dir && ./update-hub.sh --dry-run --load /tmp/topcoat-image.tar.gz $image"
  if [ "$yes" -eq 1 ]; then
    echo "   --yes does not cover production: it is always asked" >&2
  fi
  read -r -p "The dry run passed. Update PRODUCTION to $image? [y/N] " go
  [ "$go" = "y" ] || { echo "stopped: production stays as it is" >&2; exit 1; }
  ssh "$prod_ssh" "cd $prod_dir && ./update-hub.sh $image"
fi
say "done: $image"
