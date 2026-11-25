#!/usr/bin/env bash
# Back up the hub: the database and the hub's TLS volume, with a retention rule.
#
#   deploy/backup-hub.sh [--dry-run]
#
# What it writes into the backup directory (mode 600):
#   topcoat-nightly-<stamp>.dump   pg_dump -Fc of the database, checked with pg_restore --list
#   hub-tls-<stamp>.tgz            the volume with the hub CA and the key of the secret box.
#                                  Without it a restored database cannot decrypt stored tokens,
#                                  and every node has to pair again.
# What it prunes: the newest TOPCOAT_BACKUP_KEEP (14) of each nightly file are kept, and the
# newest TOPCOAT_BACKUP_KEEP_BEFORE (5) of the dumps that update-hub.sh writes before an update.
#
# Environment: TOPCOAT_ENV_FILE (default: hub.env next to this script), TOPCOAT_BACKUP_DIR
# (default: ../backup), TOPCOAT_TLS_VOLUME (default: topcoat_hub-tls).
# Run it from the systemd timer in deploy/topcoat-backup.timer, or by hand.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
env_file="${TOPCOAT_ENV_FILE:-$here/hub.env}"
backup_dir="${TOPCOAT_BACKUP_DIR:-$here/../backup}"
tls_volume="${TOPCOAT_TLS_VOLUME:-topcoat_hub-tls}"
keep_nightly="${TOPCOAT_BACKUP_KEEP:-14}"
keep_before="${TOPCOAT_BACKUP_KEEP_BEFORE:-5}"
compose=(docker compose -f "$here/docker-compose.yml" --env-file "$env_file")

dry=0
case "${1:-}" in
  "") ;;
  --dry-run) dry=1 ;;
  -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
  *) echo "unknown option: $1" >&2; exit 64 ;;
esac
[ -f "$env_file" ] || { echo "env file not found: $env_file" >&2; exit 66; }

say() { printf '== %s\n' "$*"; }

# keep the newest <n> files that match <pattern> in the backup directory, remove the rest
prune() { # <pattern> <n>
  local old
  old="$(cd "$backup_dir" && ls -1t -- $1 2>/dev/null | tail -n +"$(($2 + 1))" || true)"
  [ -n "$old" ] || return 0
  while IFS= read -r f; do
    if [ "$dry" -eq 1 ]; then say "would remove $f"; else rm -f -- "$backup_dir/$f" && say "removed $f"; fi
  done <<< "$old"
}

stamp="$(date +%Y%m%d-%H%M)"
dump="$backup_dir/topcoat-nightly-$stamp.dump"
tls="$backup_dir/hub-tls-$stamp.tgz"

if [ "$dry" -eq 1 ]; then
  say "dry run: would write $dump and $tls"
else
  umask 077
  mkdir -p "$backup_dir"
  say "database dump -> $dump"
  "${compose[@]}" exec -T db pg_dump -U topcoat -Fc topcoat > "$dump.part"
  [ -s "$dump.part" ] || { rm -f "$dump.part"; echo "the dump is empty" >&2; exit 3; }
  # a dump that cannot be listed cannot be restored: do not keep it as a backup
  if ! "${compose[@]}" exec -T db pg_restore --list < "$dump.part" > /dev/null; then
    rm -f "$dump.part"
    echo "the dump cannot be read back" >&2
    exit 4
  fi
  mv "$dump.part" "$dump"

  image="$(grep -E '^TOPCOAT_IMAGE=' "$env_file" | head -1 | cut -d= -f2-)"
  [ -n "$image" ] || { echo "TOPCOAT_IMAGE is not set in $env_file" >&2; exit 66; }
  say "TLS volume -> $tls"
  docker run --rm --entrypoint tar -v "$tls_volume":/data:ro "$image" -C /data -czf - . > "$tls.part"
  [ -s "$tls.part" ] || { rm -f "$tls.part"; echo "the TLS archive is empty" >&2; exit 5; }
  mv "$tls.part" "$tls"
fi

prune 'topcoat-nightly-*.dump' "$keep_nightly"
prune 'hub-tls-*.tgz' "$keep_nightly"
prune 'topcoat-before-*.dump' "$keep_before"

if [ "$dry" -eq 0 ]; then
  printf '%s ok %s\n' "$(date -u +%FT%TZ)" "$(basename "$dump")" > "$backup_dir/last-backup.txt"
  say "done"
fi
