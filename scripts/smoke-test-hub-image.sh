#!/usr/bin/env bash
# Smoke test for the hub image: starts Postgres + hub (TLS mode auto) and checks that the
# hub migrates, serves the API and the GUI over TLS verified against its own CA, refuses
# plain HTTP and keeps its CA across a container restart.
#   scripts/smoke-test-hub-image.sh <image>      (PG_IMAGE overrides the Postgres image)
set -euo pipefail
image="$1"
pg_image="${PG_IMAGE:-pgvector/pgvector:pg16}"
net="topcoat-smoke-$$"
work="$(mktemp -d)"
port="${SMOKE_PORT:-18443}"

# Windows curl (Schannel, e.g. Git Bash) cannot reach the revocation server for the hub's own
# CA and fails with "revocation status unknown": skip only that check there; the CA is still
# verified against the hub's own certificate (--cacert).
case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*)
    curl() { command curl --ssl-no-revoke "$@"; }
    ;;
esac

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "--- hub logs"
    docker logs topcoat-smoke-hub 2>&1 | tail -50 || true
  fi
  docker rm -f topcoat-smoke-hub topcoat-smoke-db >/dev/null 2>&1 || true
  docker volume rm -f topcoat-smoke-tls >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT

docker network create "$net" >/dev/null
docker run -d --name topcoat-smoke-db --network "$net" \
  -e POSTGRES_USER=topcoat -e POSTGRES_PASSWORD=smoke -e POSTGRES_DB=topcoat "$pg_image" >/dev/null
for _ in $(seq 60); do
  docker exec topcoat-smoke-db pg_isready -U topcoat -d topcoat >/dev/null 2>&1 && break
  sleep 1
done

start_hub() {
  docker run -d --name topcoat-smoke-hub --network "$net" -p "127.0.0.1:$port:8443" \
    -v topcoat-smoke-tls:/data/tls \
    -e TOPCOAT_DATABASE_URL=postgresql+psycopg://topcoat:smoke@topcoat-smoke-db:5432/topcoat \
    -e TOPCOAT_DEV_ADMIN_TOKEN=smoke-token \
    -e TOPCOAT_TLS_HOSTNAMES=hub.example.lan,localhost \
    "$image" >/dev/null
  for _ in $(seq 60); do
    if docker cp topcoat-smoke-hub:/data/tls/ca.pem "$work/ca.pem" >/dev/null 2>&1 &&
      curl -fsS --cacert "$work/ca.pem" "https://localhost:$port/api/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "hub did not become healthy" >&2
  return 1
}

start_hub
echo "health: $(curl -fsS --cacert "$work/ca.pem" "https://localhost:$port/api/health")"
fp1="$(curl -fsS --cacert "$work/ca.pem" "https://localhost:$port/api/tls" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ca_fingerprint"])')"
echo "CA fingerprint: $fp1"
curl -fsS --cacert "$work/ca.pem" "https://localhost:$port/" | grep -q '<div id="root">' || {
  echo "GUI index.html not served" >&2
  exit 1
}
curl -fsS --cacert "$work/ca.pem" -X POST -H "Authorization: Bearer smoke-token" \
  "https://localhost:$port/api/pairing-codes" | grep -q "$fp1" || {
  echo "pairing code does not carry the CA fingerprint" >&2
  exit 1
}
if curl -fsS -m 5 "http://localhost:$port/api/health" >/dev/null 2>&1; then
  echo "hub answered plain HTTP" >&2
  exit 1
fi
test "$(docker exec topcoat-smoke-hub id -u)" != 0 || {
  echo "hub runs as root" >&2
  exit 1
}

docker rm -f topcoat-smoke-hub >/dev/null
start_hub
fp2="$(curl -fsS --cacert "$work/ca.pem" "https://localhost:$port/api/tls" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ca_fingerprint"])')"
test "$fp1" = "$fp2" || {
  echo "CA changed across restart: $fp1 -> $fp2" >&2
  exit 1
}
echo "smoke test passed"
