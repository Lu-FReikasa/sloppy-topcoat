#!/usr/bin/env bash
# Run the hub tests, lint and formatting on a dedicated Linux machine with Docker (more cores than
# a laptop, no WSL). Only the files that differ from origin/main are shipped after the first run,
# the throw-away pgvector keeps running between runs, so a targeted run takes seconds.
#
#   scripts/dev/remote-ci.sh                    all hub tests (~2 minutes on 12 cores)
#   scripts/dev/remote-ci.sh changed            only the tests that belong to the changed files
#   scripts/dev/remote-ci.sh tests/test_x.py -k pools     any pytest arguments
#   scripts/dev/remote-ci.sh lint               ruff check + format --check
#   scripts/dev/remote-ci.sh fmt                ruff format on the changed Python files, copied back here
#   scripts/dev/remote-ci.sh reset              ship the whole tree again (after a pull or a rebase)
#   scripts/dev/remote-ci.sh stop               stop the pgvector container on the machine
#
# Configuration: TOPCOAT_CI_HOST (an ssh target) comes from the environment or from .dev/hosts.env
# (gitignored; template: scripts/dev/hosts.env.example). Optional: WORKERS (default 10),
# TOPCOAT_CI_DIR (directory on the machine, default tc-ci). Nothing of the repository's history is
# sent; the release-script tests get a throw-away git repository on branch main.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
here="$root/scripts/dev"
# shellcheck disable=SC1091
[ -f "$root/.dev/hosts.env" ] && . "$root/.dev/hosts.env"
host="${TOPCOAT_CI_HOST:?set TOPCOAT_CI_HOST (environment or .dev/hosts.env)}"
dir="${TOPCOAT_CI_DIR:-tc-ci}"
cd "$root"

mode="tests"
case "${1:-}" in
  changed | lint | fmt | reset | stop) mode="$1"; shift ;;
esac

if [ "$mode" = "stop" ]; then
  ssh "$host" "docker rm -f tc-ci-pg >/dev/null 2>&1; echo stopped"
  exit 0
fi

# --- ship: the full tree once, afterwards only the delta against origin/main -------------------
ship_full() {
  local list
  list="$(mktemp)"
  git ls-files --cached --others --exclude-standard -z | grep -zv '^nul$' > "$list"
  ssh "$host" "mkdir -p ~/$dir && docker run --rm -v ~/$dir:/w alpine rm -rf /w/src /w/manifest && mkdir -p ~/$dir/src"
  tar --null -T "$list" -cf - 2>/dev/null | ssh "$host" "tar -xmf - -C ~/$dir/src"
  ssh "$host" "cd ~/$dir/src && git init -q -b main && git add -A && git -c user.email=ci@example.lan -c user.name=ci commit -qm ci && : > ~/$dir/manifest"
  rm -f "$list"
}

ship_delta() {
  local now prev all gone have
  now="$(mktemp)"; prev="$(mktemp)"; all="$(mktemp)"; gone="$(mktemp)"; have="$(mktemp)"
  { git diff --name-only origin/main; git ls-files --others --exclude-standard; } | grep -v '^nul$' | sort -u > "$now"
  ssh "$host" "cat ~/$dir/manifest 2>/dev/null || true" > "$prev"
  # what was shipped last time is sent again, so a file that went back to origin/main is restored
  sort -u "$now" "$prev" > "$all"
  : > "$gone"; : > "$have"
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if [ -e "$f" ]; then printf '%s\n' "$f" >> "$have"; else printf '%s\n' "$f" >> "$gone"; fi
  done < "$all"
  if [ -s "$gone" ]; then
    ssh "$host" "cd ~/$dir/src && xargs -r rm -f" < "$gone"
  fi
  if [ -s "$have" ]; then
    tar -T "$have" -cf - 2>/dev/null | ssh "$host" "tar -xmf - -C ~/$dir/src"
  fi
  ssh "$host" "cat > ~/$dir/manifest" < "$now"
  CHANGED_FILE="$now"
  rm -f "$prev" "$all" "$gone" "$have"
}

if [ "$mode" = "reset" ] || ! ssh "$host" "test -f ~/$dir/manifest"; then
  ship_full
  CHANGED_FILE="$(mktemp)"
  { git diff --name-only origin/main; git ls-files --others --exclude-standard; } | grep -v '^nul$' | sort -u > "$CHANGED_FILE"
  ssh "$host" "cat > ~/$dir/manifest" < "$CHANGED_FILE"
  [ "$mode" = "reset" ] && { echo "tree shipped"; exit 0; }
  ship_delta   # the first run may already have uncommitted changes the full tar did not see
else
  ship_delta
fi
scp -q "$here/remote-hub-tests.sh" "$host:$dir/remote-hub-tests.sh"

uvrun() { # uvrun <shell command inside the hub directory>
  ssh "$host" "mkdir -p ~/$dir/venv ~/$dir/uvcache && docker run --rm -v ~/$dir/src:/src -v ~/$dir/venv:/venv -v ~/$dir/uvcache:/uvcache -e UV_PROJECT_ENVIRONMENT=/venv -e UV_CACHE_DIR=/uvcache -e UV_LINK_MODE=copy -w /src/hub tc-ci-uv bash -c 'uv sync --quiet --frozen; $1'"
}
hub_py_changed() { grep '^hub/.*\.py$' "$CHANGED_FILE" | while IFS= read -r f; do [ -e "$f" ] && printf '%s\n' "${f#hub/}"; done; }

case "$mode" in
  lint)
    uvrun 'uv run --frozen ruff check src tests && uv run --frozen ruff format --check src tests'
    ;;
  fmt)
    files="$(hub_py_changed | tr '\n' ' ')"
    if [ -z "$files" ]; then echo "no changed Python files"; exit 0; fi
    uvrun "uv run --frozen ruff check --fix -q $files; uv run --frozen ruff format $files"
    # copy the formatted files back (the Windows tree keeps its own line endings out of it: ruff writes LF,
    # git's autocrlf turns it into the working-tree style on the next add)
    # shellcheck disable=SC2086
    ssh "$host" "cd ~/$dir/src/hub && tar -cf - $files" | tar -xf - -C "$root/hub"
    echo "formatted: $files"
    ;;
  changed)
    sel=""
    while IFS= read -r f; do
      case "$f" in
        hub/tests/test_*.py) [ -e "$f" ] && sel="$sel ${f#hub/}" ;;
        hub/src/topcoat_hub/*.py)
          stem="$(basename "$f" .py)"
          for t in hub/tests/test_${stem}*.py; do [ -e "$t" ] && sel="$sel ${t#hub/}"; done ;;
        hub/src/topcoat_hub/*/*.py)
          stem="$(basename "$f" .py)"
          for t in hub/tests/test_${stem}*.py; do [ -e "$t" ] && sel="$sel ${t#hub/}"; done ;;
      esac
    done < "$CHANGED_FILE"
    sel="$(printf '%s\n' $sel | sort -u | tr '\n' ' ')"
    if [ -z "$sel" ]; then
      if ! grep -q '^hub/' "$CHANGED_FILE"; then echo "nothing under hub/ changed: no hub test to run"; exit 0; fi
      echo "no test is named after the changed files; running everything"
    fi
    # shellcheck disable=SC2086
    ssh "$host" "WORKERS=${WORKERS:-10} TOPCOAT_CI_DIR=\$HOME/$dir bash ~/$dir/remote-hub-tests.sh $sel"
    ;;
  tests)
    ssh "$host" "WORKERS=${WORKERS:-10} TOPCOAT_CI_DIR=\$HOME/$dir bash ~/$dir/remote-hub-tests.sh $*"
    ;;
esac
