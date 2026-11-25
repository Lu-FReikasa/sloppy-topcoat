#!/bin/sh
# Installed on the dev node as /usr/local/sbin/topcoat-live-import (root-owned, 0755) and run as the
# workshop's user through one sudo rule. It takes the commit the hub runs (a git bundle the
# overlay deploy copied to a fixed path) into the workshop's local origin, so the assistant reads
# the live code and not the last state of GitHub:
#
#   branch "live"   always the hub's commit
#   branch "main"   moved forward to it only as a fast-forward; a main with commits of its own
#                   (work taken over on the hub's page) is left as it is and the script says so
#
# Afterwards repo.sync in Topcoat brings the workshop's clone up to date (the node's own step).
# Nothing here takes an argument: the paths are fixed, so the sudo rule allows exactly this.
#
# Install (once, as root on the dev node; the paths are the defaults of the node agent):
#   install -o root -g root -m 0755 devnode-live-import.sh /usr/local/sbin/topcoat-live-import
#   echo '<ssh-user> ALL=(topcoat-dev) NOPASSWD: /usr/local/sbin/topcoat-live-import' \
#     > /etc/sudoers.d/topcoat-live-import && chmod 0440 /etc/sudoers.d/topcoat-live-import
#   visudo -cf /etc/sudoers.d/topcoat-live-import
set -eu

ORIGIN=/srv/topcoat/node-data/workshop/.origin/sloppy-topcoat-local.git
BUNDLE=/var/tmp/topcoat-live.bundle
WORKSHOP_USER=topcoat-dev

[ "$(id -un)" = "$WORKSHOP_USER" ] || { echo "run as $WORKSHOP_USER (sudo -u $WORKSHOP_USER)" >&2; exit 2; }
# sudo keeps the caller's HOME; git must not read the caller's configuration
HOME=$(getent passwd "$WORKSHOP_USER" | cut -d: -f6)
export HOME
[ -d "$ORIGIN" ] || { echo "no local origin at $ORIGIN" >&2; exit 3; }
[ -f "$BUNDLE" ] || { echo "no bundle at $BUNDLE" >&2; exit 3; }

git="git --git-dir=$ORIGIN"
$git bundle verify -q "$BUNDLE" >/dev/null 2>&1 || { echo "the bundle at $BUNDLE is not valid" >&2; exit 4; }
$git fetch --quiet --no-tags "$BUNDLE" HEAD
new=$($git rev-parse FETCH_HEAD)
$git update-ref refs/heads/live "$new"

old=$($git rev-parse -q --verify refs/heads/main || true)
if [ -z "$old" ]; then
  $git update-ref refs/heads/main "$new"
  echo "live=$new main=$new (new)"
elif [ "$old" = "$new" ]; then
  echo "live=$new main=$new (already there)"
elif $git merge-base --is-ancestor "$old" "$new"; then
  $git update-ref refs/heads/main "$new" "$old"
  echo "live=$new main=$new (fast-forward from $old)"
else
  echo "live=$new main=$old (main has commits of its own: only live moved)"
fi
