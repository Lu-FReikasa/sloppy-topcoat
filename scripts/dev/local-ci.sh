#!/usr/bin/env bash
# Local mirror of the CI jobs, so a push does not turn red for something a laptop or a
# cloud session could have caught. See docs/DEVELOPMENT.md ("Check before you push").
#
#   scripts/dev/local-ci.sh [--fast] [part ...]
#
# parts: node web hub e2e   (default: all; --fast skips the slow ones: -race, full pytest, e2e)
#
# Not covered (CI only): Windows tests (node-windows), Docker image + smoke test.
# `hub` and `e2e` need a PostgreSQL 16 server with pgvector (postgresql-16 + postgresql-16-pgvector);
# the script starts a throw-away cluster in a temp dir on 127.0.0.1:55432 and removes it again.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FAST=0
PARTS=()
for a in "$@"; do
  case "$a" in
    --fast) FAST=1 ;;
    node | web | hub | e2e) PARTS+=("$a") ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done
[ ${#PARTS[@]} -eq 0 ] && PARTS=(node web hub e2e)

FAILED=()
SUMMARY=()
run() { # run <label> <dir> <cmd...>
  local label="$1" dir="$2"
  shift 2
  local t0=$SECONDS
  echo "::: $label"
  if (cd "$ROOT/$dir" && "$@"); then
    SUMMARY+=("ok    $((SECONDS - t0))s  $label")
  else
    SUMMARY+=("FAIL  $((SECONDS - t0))s  $label")
    FAILED+=("$label")
  fi
}

PGPORT=55432
PGDIR=""
pg_start() {
  [ -n "$PGDIR" ] && return 0
  local bin
  bin="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"
  if [ -z "$bin" ]; then
    echo "no PostgreSQL server found (apt install postgresql postgresql-16-pgvector)" >&2
    return 1
  fi
  PGDIR="$(mktemp -d /tmp/topcoat-pg.XXXXXX)"
  local as=""
  if [ "$(id -u)" = 0 ]; then
    chown postgres "$PGDIR"
    as="su postgres -c"
  fi
  local sh_run
  sh_run() { if [ -n "$as" ]; then su postgres -c "$*"; else bash -c "$*"; fi; }
  sh_run "$bin/initdb -D $PGDIR/data -U topcoat --auth=trust >/dev/null" || return 1
  sh_run "$bin/pg_ctl -D $PGDIR/data -o '-p $PGPORT -k $PGDIR -c listen_addresses=127.0.0.1 -c fsync=off -c synchronous_commit=off -c full_page_writes=off' -l $PGDIR/log -w start >/dev/null" || return 1
  sh_run "$bin/createdb -h 127.0.0.1 -p $PGPORT -U topcoat topcoat_test" || return 1
  PG_BIN="$bin"
  export TOPCOAT_TEST_DATABASE_URL="postgresql+psycopg://topcoat@127.0.0.1:$PGPORT/topcoat_test"
  export TOPCOAT_DATABASE_URL="$TOPCOAT_TEST_DATABASE_URL"
  # extension must exist before the migrations run (superuser = topcoat here)
  "$bin/psql" -h 127.0.0.1 -p $PGPORT -U topcoat -d topcoat_test -qc "CREATE EXTENSION IF NOT EXISTS vector" || return 1
}
pg_stop() {
  [ -z "$PGDIR" ] && return 0
  if [ "$(id -u)" = 0 ]; then
    su postgres -c "$PG_BIN/pg_ctl -D $PGDIR/data -m immediate stop >/dev/null" || true
  else
    "$PG_BIN/pg_ctl" -D "$PGDIR/data" -m immediate stop >/dev/null || true
  fi
  rm -rf "$PGDIR"
}
trap pg_stop EXIT

for part in "${PARTS[@]}"; do
  case "$part" in
    node)
      run "node: go vet" node go vet ./...
      run "node: go vet (windows)" node env GOOS=windows go vet ./...
      if [ $FAST -eq 1 ]; then
        run "node: go test" node go test ./...
      else
        run "node: go test -race" node go test -race ./...
      fi
      if command -v golangci-lint >/dev/null; then
        run "node: golangci-lint" node golangci-lint run
        run "node: golangci-lint (windows)" node env GOOS=windows golangci-lint run
      else
        SUMMARY+=("skip  -     node: golangci-lint (install: cd node && go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.14.0)")
      fi
      run "node: cross-compile" node env CGO_ENABLED=0 ../scripts/build-node-release.sh "local" /tmp/topcoat-node-local
      ;;
    web)
      [ -d "$ROOT/web/node_modules" ] || run "web: npm ci" web npm ci
      run "web: lint" web npm run lint
      run "web: typecheck" web npm run typecheck
      run "web: vitest" web npm test
      run "web: build" web npm run build
      ;;
    hub)
      run "hub: uv sync" hub uv sync --frozen
      run "hub: ruff check" hub uv run ruff check .
      run "hub: ruff format --check" hub uv run ruff format --check .
      if [ $FAST -eq 1 ]; then
        SUMMARY+=("skip  -     hub: pytest/migrations (--fast)")
      elif pg_start; then
        run "hub: migrations up/down/check" hub bash -c \
          "uv run alembic upgrade head && uv run alembic check && uv run alembic downgrade base"
        run "hub: pytest" hub uv run pytest -q -x -n "${TOPCOAT_PYTEST_WORKERS:-4}"
      else
        SUMMARY+=("FAIL  -     hub: no database"); FAILED+=("hub db")
      fi
      ;;
    e2e)
      if [ $FAST -eq 1 ]; then
        SUMMARY+=("skip  -     e2e (--fast)")
      elif pg_start; then
        run "e2e: build node" node env CGO_ENABLED=0 go build -o bin/topcoat-node ./cmd/topcoat-node
        run "e2e: integration test" hub env TOPCOAT_NODE_BIN="$ROOT/node/bin/topcoat-node" \
          uv run pytest -m integration -q -x
      else
        SUMMARY+=("FAIL  -     e2e: no database"); FAILED+=("e2e db")
      fi
      ;;
  esac
done

echo
echo "=== summary (CI-only: windows tests, docker image + smoke test) ==="
printf '%s\n' "${SUMMARY[@]}"
[ ${#FAILED[@]} -eq 0 ] && echo "ALL OK" || { echo "FAILED: ${FAILED[*]}"; exit 1; }
