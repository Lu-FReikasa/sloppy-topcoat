#!/usr/bin/env bash
# Put the commit the hub runs into the dev node's workshop, so the assistant reads the live code.
# Bundles HEAD of this checkout, copies it to the dev node and runs the import there
# (scripts/dev/devnode-live-import.sh, installed once as /usr/local/sbin/topcoat-live-import).
# deploy-overlay.sh calls it after a successful push; it can also run alone:
#
#   scripts/dev/push-live-to-devnode.sh
#
# Then the workshop's clone moves onto it through Topcoat's own steps (workshop_live.py on the hub host).
#
# Environment (or .dev/hosts.env): TOPCOAT_DEVNODE_HOST  ssh target of the dev node (unset = skip),
# TOPCOAT_OVERLAY_HOST  ssh target of the hub host
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
[ -f "$root/.dev/hosts.env" ] && . "$root/.dev/hosts.env"
host="${TOPCOAT_DEVNODE_HOST:-}"
if [ -z "$host" ]; then
  echo "TOPCOAT_DEVNODE_HOST is not set: the live code was not put on the dev node" >&2
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git -C "$root" bundle create --quiet "$tmp/live.bundle" HEAD
scp -q -o BatchMode=yes "$tmp/live.bundle" "$host:/var/tmp/topcoat-live.bundle.new"
ssh -o BatchMode=yes "$host" 'chmod 0644 /var/tmp/topcoat-live.bundle.new &&
  mv -f /var/tmp/topcoat-live.bundle.new /var/tmp/topcoat-live.bundle &&
  sudo -n -u topcoat-dev /usr/local/sbin/topcoat-live-import'

# the clone the assistant reads follows through Topcoat's own steps (repo.sync, repo.start live)
hub="${TOPCOAT_OVERLAY_HOST:-}"
if [ -n "$hub" ]; then
  ssh -o BatchMode=yes "$hub" 'TOPCOAT_ENV_FILE=$HOME/topcoat/deploy/hub.env python3 -' <"$root/scripts/dev/workshop_live.py"
else
  echo "TOPCOAT_OVERLAY_HOST is not set: run repo.sync and repo.start (topic live) in Topcoat yourself" >&2
fi
