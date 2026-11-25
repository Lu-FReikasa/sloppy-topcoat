#!/usr/bin/env bash
# Clear the assistant's memory of what it learned from chats, with a dump first.
#
#   deploy/memory-reset.sh [--dry-run]
#
# What goes (status "deleted", nothing is dropped from the database): every note about the person,
# their preferences and their servers, and every episode. What stays: the rules and styles the person
# gave the assistant (kind "rule" and "style"), and everything that is not a note. The versions of the
# notes stay too, so the dump is not the only way back.
#
# A dump of the memory tables is written to the backup directory first (mode 600, checked with
# pg_restore --list); without it nothing is changed.
#
# Environment: TOPCOAT_DB_CONTAINER (default: topcoat-db-1), TOPCOAT_BACKUP_DIR (default: ../backup).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
container="${TOPCOAT_DB_CONTAINER:-topcoat-db-1}"
backup_dir="${TOPCOAT_BACKUP_DIR:-$here/../backup}"
dry=0
case "${1:-}" in
  "") ;;
  --dry-run) dry=1 ;;
  -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
  *) echo "unknown option: $1" >&2; exit 64 ;;
esac

say() { printf '== %s\n' "$*"; }
psql() { docker exec -i "$container" psql -U topcoat topcoat -v ON_ERROR_STOP=1 "$@"; }

say "notes by kind and status now"
psql -c "select kind, status, count(*) from memory_items group by 1, 2 order by 1, 2"

if [ "$dry" -eq 1 ]; then
  say "would delete: $(psql -tAc "select count(*) from memory_items where status <> 'deleted' and kind not in ('rule', 'style')") notes"
  say "dry run: nothing was changed"
  exit 0
fi

mkdir -p "$backup_dir"
dump="$backup_dir/memory-before-reset-$(date +%Y%m%d-%H%M%S).dump"
say "dump of the memory tables -> $dump"
docker exec "$container" pg_dump -U topcoat -Fc \
  -t 'memory_*' -t chat_digests -t chat_learned topcoat > "$dump"
chmod 600 "$dump"
docker exec -i "$container" pg_restore --list < "$dump" > /dev/null \
  || { echo "the dump does not read back - not deleting" >&2; exit 3; }

say "deleting the notes"
psql -tAc "update memory_items set status = 'deleted', updated_at = now()
           where status <> 'deleted' and kind not in ('rule', 'style') returning 1" | wc -l \
  | xargs -I{} printf '== %s notes marked deleted\n' {}
say "notes by kind and status now"
psql -c "select kind, status, count(*) from memory_items group by 1, 2 order by 1, 2"
