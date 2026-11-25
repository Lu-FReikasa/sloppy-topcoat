#!/bin/sh
# `pytest` of the workshop test image (see Dockerfile). Runs as an arbitrary unprivileged user.
set -u
PY=/opt/venv/bin/python

if [ ! -f /work/hub/pyproject.toml ] || [ ! -d /work/hub/src/topcoat_hub ]; then
    exec "$PY" -m pytest -p no:cacheprovider "$@"
fi

# The hub tests need PostgreSQL with pgvector. The container has no network and a read-only root,
# so the database lives in /tmp and is gone with the container.
PGBIN=$(ls -d /usr/lib/postgresql/*/bin | tail -n 1)
export HOME=/tmp
ME=$(id -u)
# PostgreSQL refuses to run as a user without an entry in the passwd file.
printf 'tester:x:%s:%s:tester:/tmp:/bin/sh\n' "$ME" "$(id -g)" > /tmp/passwd
printf 'tester:x:%s:\n' "$(id -g)" > /tmp/group
export NSS_WRAPPER_PASSWD=/tmp/passwd NSS_WRAPPER_GROUP=/tmp/group
export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libnss_wrapper.so

"$PGBIN/initdb" -D /tmp/pgdata -U topcoat --auth=trust --no-sync -E UTF8 --locale=C.UTF-8 \
    > /tmp/initdb.log 2>&1 || { cat /tmp/initdb.log; exit 70; }
"$PGBIN/pg_ctl" -D /tmp/pgdata -l /tmp/pg.log -w -t 60 \
    -o "-c listen_addresses=127.0.0.1 -c unix_socket_directories=/tmp -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c shared_buffers=128MB -c max_connections=200" \
    start > /dev/null || { cat /tmp/pg.log; exit 71; }
"$PGBIN/createdb" -h 127.0.0.1 -U topcoat topcoat_test || exit 72

export TOPCOAT_TEST_DATABASE_URL=postgresql+psycopg://topcoat@127.0.0.1:5432/topcoat_test
# The workshop copy has no .git, and the release-script tests need a repository. /work may not be
# writable for this user (a rootless Docker daemon maps the user to another owner than the copy's),
# so the throw-away repository is made in a copy of the tree in /tmp, and the tests run there.
WORK=/work
if [ ! -e /work/.git ]; then
  WORK=/tmp/work
  mkdir -p "$WORK" && cp -R --no-preserve=ownership /work/. "$WORK"/ || exit 74
  ( cd "$WORK" && git init -q && git add -A >/dev/null 2>&1 \
    && git -c user.name=workshop -c user.email=workshop@example.lan commit -q -m workshop \
    && git update-ref refs/remotes/origin/main HEAD ) >/dev/null 2>&1 || true
fi
export PYTHONPATH="$WORK/hub/src" TOPCOAT_PROTO_ROOT="$WORK/proto"
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$WORK"
cd "$WORK/hub" || exit 73
"$PY" -m pytest -p no:cacheprovider -n "${WORKSHOP_PYTEST_WORKERS:-2}" "$@"
rc=$?
"$PGBIN/pg_ctl" -D /tmp/pgdata -m immediate stop > /dev/null 2>&1

# The web checks of the same repository (the project's own scripts): typecheck, lint, tests. The
# packages are in the image; the root file system is read-only, so the copy gets a writable
# node_modules of links to them (the tools write their caches there). A failure in either part
# fails the run.
if [ -f "$WORK/web/package.json" ] && [ -d /opt/web/node_modules ]; then
  echo "=== web: lint, typecheck, tests"
  web=0
  ( cd "$WORK/web" && mkdir -p node_modules \
    && for pkg in /opt/web/node_modules/* /opt/web/node_modules/.[!.]*; do \
         ln -sfn "$pkg" "node_modules/$(basename "$pkg")"; done \
    && npm run --silent lint && npm run --silent typecheck && npm test ) || web=$?
  [ "$rc" -eq 0 ] && rc=$web
fi
exit $rc
