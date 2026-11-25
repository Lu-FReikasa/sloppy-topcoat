#!/usr/bin/env bash
# Install the roll-out of issue fixes on the hub host (ARCHITECTURE D104), once, as root:
#
#   sudo deploy/fix-overlay/install.sh /home/<deploy user>/topcoat
#
# It installs the fixed script, the two systemd units and the spool directory: request/ is
# writable by the hub's container user (uid 10001), done/ only by root. The next overlay deploy
# (scripts/dev/deploy-overlay.sh) mounts the spool into the hub as /spool.
set -euo pipefail
dir="${1:?the directory that holds deploy/ and overlay/}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
spool="$dir/hub-spool"
hub_uid="${TOPCOAT_HUB_UID:-10001}"
owner="$(stat -c %U "$dir")"

install -o root -g root -m 0755 "$here/hub-fix-overlay.sh" /usr/local/sbin/topcoat-fix-overlay
install -d -o "$owner" -g "$owner" -m 0755 "$spool"
install -d -o "$hub_uid" -g "$owner" -m 0750 "$spool/request"
install -d -o root -g root -m 0755 "$spool/done"
for unit in topcoat-fix-overlay.path topcoat-fix-overlay.service; do
  sed -e "s|@DIR@|$dir|g" -e "s|@SPOOL@|$spool|g" "$here/$unit" >"/etc/systemd/system/$unit"
  chmod 0644 "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now topcoat-fix-overlay.path
echo "installed: spool $spool, script /usr/local/sbin/topcoat-fix-overlay"
