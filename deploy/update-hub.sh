#!/usr/bin/env bash
# Update the hub to another image with a way back.
#
#   deploy/update-hub.sh [options] <image>
#     --load <file.tar.gz|->   docker-load the image first (a file, or "-" for a stream on stdin)
#     --dry-run                run the checks only, change nothing
#     --db-revision <rev>      pretend the database is at <rev> (for testing the check)
#     --skip-revision-check    deploy even if the image does not know the database revision
#
# What it does, in this order, and stops at the first problem:
#   1. the image exists locally (after --load);
#   2. the image contains the migration the database is at (a database that is ahead of an
#      image makes the hub refuse to start - this is the check that would have caught it);
#   3. a database dump is written to the backup directory;
#   4. TOPCOAT_IMAGE in the env file is switched and only the hub is recreated;
#   5. the hub must answer its health URL within TOPCOAT_HEALTH_WAIT seconds - if not, the previous
#      image is put back and checked the same way (exit 1);
#   6. only then are older hub images removed: the new and the previous one always stay.
#
# Environment: TOPCOAT_ENV_FILE (default: hub.env next to this script), TOPCOAT_BACKUP_DIR
# (default: ../backup), TOPCOAT_HEALTH_URL (default: https://127.0.0.1:8443/api/health),
# TOPCOAT_HEALTH_WAIT (default: 90), TOPCOAT_IMAGE_REPO (default: sloppy-topcoat-hub).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
env_file="${TOPCOAT_ENV_FILE:-$here/hub.env}"
backup_dir="${TOPCOAT_BACKUP_DIR:-$here/../backup}"
health_url="${TOPCOAT_HEALTH_URL:-https://127.0.0.1:8443/api/health}"
health_wait="${TOPCOAT_HEALTH_WAIT:-90}"
image_repo="${TOPCOAT_IMAGE_REPO:-sloppy-topcoat-hub}"
compose=(docker compose -f "$here/docker-compose.yml" --env-file "$env_file")

load="" dry=0 fake_rev="" skip_check=0 new_image=""
while [ $# -gt 0 ]; do
  case "$1" in
    --load) load="${2:?--load needs a file or -}"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    --db-revision) fake_rev="${2:?--db-revision needs a value}"; shift 2 ;;
    --skip-revision-check) skip_check=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 64 ;;
    *) new_image="$1"; shift ;;
  esac
done
[ -n "$new_image" ] || { echo "usage: $0 [options] <image>   (see --help)" >&2; exit 64; }
[ -f "$env_file" ] || { echo "env file not found: $env_file" >&2; exit 66; }

say() { printf '== %s\n' "$*"; }
current_image() { grep -E '^TOPCOAT_IMAGE=' "$env_file" | head -1 | cut -d= -f2-; }

wait_healthy() {
  local waited=0
  while [ "$waited" -lt "$health_wait" ]; do
    if curl -ksf -m 4 -o /dev/null "$health_url"; then return 0; fi
    sleep 3; waited=$((waited + 3))
  done
  return 1
}

image_knows_revision() { # <image> <revision>
  docker run --rm --entrypoint sh "$1" -c 'grep -rlE "^revision *(: *str *)?= *[\"'\'']'"$2"'[\"'\'']" /app/hub/migrations/versions' >/dev/null 2>&1
}

if [ -n "$load" ]; then
  say "loading the image"
  if [ "$load" = "-" ]; then gunzip | docker load; else gunzip -c "$load" | docker load; fi
fi
docker image inspect "$new_image" >/dev/null 2>&1 || { echo "image not found locally: $new_image" >&2; exit 66; }

prev_image="$(current_image)"
[ -n "$prev_image" ] || { echo "TOPCOAT_IMAGE is not set in $env_file" >&2; exit 66; }
say "running: $prev_image  ->  new: $new_image"

if [ -n "$fake_rev" ]; then
  db_rev="$fake_rev"
else
  db_rev="$("${compose[@]}" exec -T db psql -U topcoat topcoat -tAc 'select version_num from alembic_version' 2>/dev/null | tr -d '[:space:]' || true)"
fi
if [ -z "$db_rev" ]; then
  say "the database has no migration revision yet (a fresh database): revision check skipped"
elif [ "$skip_check" -eq 1 ]; then
  say "database revision $db_rev: check skipped on request"
elif image_knows_revision "$new_image" "$db_rev"; then
  say "database revision $db_rev: the new image knows it"
else
  echo "REFUSED: the database is at revision $db_rev, which $new_image does not contain." >&2
  echo "         This image is older than a build that was deployed before - deploy a newer one." >&2
  exit 2
fi

if [ "$dry" -eq 1 ]; then say "dry run: nothing was changed"; exit 0; fi

mkdir -p "$backup_dir"
tag="$(printf '%s' "$new_image" | tr -c 'A-Za-z0-9._-' '_')"
dump="$backup_dir/topcoat-before-$tag.dump"
say "database dump -> $dump"
"${compose[@]}" exec -T db pg_dump -U topcoat -Fc topcoat > "$dump"
[ -s "$dump" ] || { echo "the dump is empty - not switching" >&2; exit 3; }

switch_to() { # <image>
  cp "$env_file" "$env_file.before-update"
  if grep -qE '^TOPCOAT_IMAGE=' "$env_file"; then
    sed -i "s|^TOPCOAT_IMAGE=.*|TOPCOAT_IMAGE=$1|" "$env_file"
  else
    printf 'TOPCOAT_IMAGE=%s\n' "$1" >> "$env_file"
  fi
  "${compose[@]}" up -d hub >/dev/null
}

say "switching the hub"
switch_to "$new_image"
if wait_healthy; then
  say "the hub answers: update done"
else
  echo "the new hub did not answer within ${health_wait}s - going back to $prev_image" >&2
  "${compose[@]}" logs --tail 25 hub >&2 || true
  switch_to "$prev_image"
  if wait_healthy; then echo "rolled back to $prev_image, the hub answers again" >&2; else echo "ROLLBACK ALSO FAILED - check: ${compose[*]} logs hub" >&2; fi
  exit 1
fi

for img in $(docker images --format '{{.Repository}}:{{.Tag}}' | grep "^$image_repo:" || true); do
  if [ "$img" != "$new_image" ] && [ "$img" != "$prev_image" ]; then docker rmi "$img" >/dev/null 2>&1 && say "removed old image $img"; fi
done
say "kept for the way back: $prev_image"
