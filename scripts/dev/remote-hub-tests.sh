#!/usr/bin/env bash
# The part of remote-ci.sh that runs on the test machine: a throw-away pgvector server and the hub
# suite in a container (Python 3.12 + uv + git), with many pytest workers. Not meant to be called by
# hand; see remote-ci.sh. Needs Docker. Extra arguments go to pytest.
set -uo pipefail
base="${TOPCOAT_CI_DIR:-$HOME/tc-ci}"
cd "$base/src/hub"
if ! docker image inspect tc-ci-uv >/dev/null 2>&1; then
  mkdir -p "$base/img"
  printf 'FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim\nRUN apt-get update -qq && apt-get install -y -qq git >/dev/null && git config --system --add safe.directory "*"\n' > "$base/img/Dockerfile"
  docker build -q -t tc-ci-uv "$base/img" >/dev/null
fi
# The database container stays up between runs (remote-ci.sh stop removes it); a run reuses it.
if ! docker exec tc-ci-pg pg_isready -p 55432 -U topcoat >/dev/null 2>&1; then
docker rm -f tc-ci-pg >/dev/null 2>&1
docker run -d --name tc-ci-pg --network host -e POSTGRES_USER=topcoat -e POSTGRES_PASSWORD=topcoat \
  -e POSTGRES_DB=topcoat_test --tmpfs /var/lib/postgresql/data:rw,size=4g --shm-size=1g pgvector/pgvector:pg16 \
  postgres -p 55432 -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=600 >/dev/null
for _ in $(seq 1 60); do docker exec tc-ci-pg pg_isready -p 55432 -U topcoat >/dev/null 2>&1 && break; sleep 1; done
fi
mkdir -p "$base/venv" "$base/uvcache"
docker run --rm --network host -v "$base/src:/src" -v "$base/venv:/venv" -v "$base/uvcache:/uvcache" \
  -e UV_PROJECT_ENVIRONMENT=/venv -e UV_CACHE_DIR=/uvcache -e UV_LINK_MODE=copy \
  -e TOPCOAT_TEST_DATABASE_URL=postgresql+psycopg://topcoat:topcoat@127.0.0.1:55432/topcoat_test \
  -w /src/hub tc-ci-uv \
  bash -c 'uv sync --quiet --frozen 2>&1 | tail -3; uv run --frozen pytest -q -n "${WORKERS:-10}" -p no:cacheprovider "$@"' _ "$@"
exit $?
