#!/usr/bin/env bash
# setup-readshell.sh — set up (or remove) the read-only shell of a tool node. Run as root, once per
# server. Idempotent: running it again changes only what differs.
#
#   sudo bash setup-readshell.sh              install (default)
#   sudo bash setup-readshell.sh audit        what could the read user still read? (review the list!)
#   sudo bash setup-readshell.sh status       is it installed and running
#   sudo bash setup-readshell.sh uninstall    the way back (the user stays; see below)
#
# What it creates (nothing else is touched):
#   user      topcoat-ro        system user, no home, no login shell, NOT in sudo/docker/adm
#   socket    /run/topcoat-readshell.sock   (root:$NODE_GROUP 0660) — only the node may connect
#   units     topcoat-readshell.socket, topcoat-readshell@.service  (one service per command)
#   wrapper   /usr/local/lib/topcoat-readshell/run
#
# "Read-only" is the operating system's job: the user has no write permission anywhere, the unit
# is sandboxed (no network, no /home, no /root, no node data, no privileges), and systemd kills
# every command after RuntimeMaxSec. Nothing here is a command list.
#
# Settings (environment variables, all optional):
#   NODE_GROUP    group of the topcoat-node service            (default topcoat-node)
#   READ_GROUPS   extra groups of the read user, space-separated (default systemd-journal; "adm"
#                 adds /var/log/*, which may hold more than you want to show — audit first)
#   HIDE_PATHS    more directories to make invisible to it, space-separated (e.g. the compose
#                 projects with .env files); written to a systemd drop-in. Each path gets a
#                 "-" prefix (a missing path is ignored, not an error)
#
# UNVERIFIED on real hardware: the units were written for systemd 249+ (Ubuntu 22.04 and later).
set -euo pipefail

RO_USER=topcoat-ro
NODE_GROUP=${NODE_GROUP:-topcoat-node}
READ_GROUPS=${READ_GROUPS:-systemd-journal}
HIDE_PATHS=${HIDE_PATHS:-}
LIB=/usr/local/lib/topcoat-readshell
SOCK=/run/topcoat-readshell.sock
UNITDIR=/etc/systemd/system

die() { echo "error: $*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0 ...)"; }

write_if_changed() { # path mode  (content on stdin)
	local path=$1 mode=$2 tmp
	tmp=$(mktemp)
	cat >"$tmp"
	if [ -f "$path" ] && cmp -s "$tmp" "$path"; then
		rm -f "$tmp"
		return 0
	fi
	install -m "$mode" "$tmp" "$path"
	rm -f "$tmp"
	echo "wrote $path"
	CHANGED=1
}
CHANGED=0

do_install() {
	need_root
	command -v systemctl >/dev/null || die "systemd is required"
	getent group "$NODE_GROUP" >/dev/null || die "group $NODE_GROUP does not exist (NODE_GROUP=... names the group of the topcoat-node service)"

	if ! id "$RO_USER" >/dev/null 2>&1; then
		useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$RO_USER"
		echo "created user $RO_USER"
	fi
	for g in $READ_GROUPS; do
		getent group "$g" >/dev/null || die "group $g does not exist"
		case $g in docker | sudo | root | wheel | shadow | disk | lxd | libvirt)
			die "refusing to put $RO_USER into group $g (that is root on this machine)" ;;
		esac
		id -nG "$RO_USER" | tr ' ' '\n' | grep -qx "$g" || { usermod -aG "$g" "$RO_USER"; echo "added $RO_USER to $g"; }
	done
	# the groups must be exactly the primary group plus READ_GROUPS: nothing it got earlier stays
	for g in $(id -nG "$RO_USER"); do
		[ "$g" = "$RO_USER" ] && continue
		case " $READ_GROUPS " in *" $g "*) ;; *)
			gpasswd -d "$RO_USER" "$g" >/dev/null && echo "removed $RO_USER from $g (not in READ_GROUPS)" ;;
		esac
	done
	for g in docker sudo root wheel shadow disk lxd libvirt; do
		if id -nG "$RO_USER" | tr ' ' '\n' | grep -qx "$g"; then die "$RO_USER is in group $g — remove it first"; fi
	done
	if sudo -n -l -U "$RO_USER" 2>/dev/null | grep -qE 'may run the following|NOPASSWD'; then
		die "sudo rules exist for $RO_USER (sudo -l -U $RO_USER) — remove them"
	fi

	mkdir -p "$LIB"
	chown root:root "$LIB"
	chmod 755 "$LIB"
	write_if_changed "$LIB/run" 0755 <<'WRAPPER'
#!/bin/bash
# One command per connection. Protocol: line 1 = a 32-hex marker, the rest up to the end of the
# stream = the command. Output = stdout+stderr of the command, then "\n<marker>:<exit code>\n".
set -u
IFS= read -r -t 5 marker || exit 2
case $marker in
*[!0-9a-f]*) exit 2 ;;
esac
[ "${#marker}" -eq 32 ] || exit 2
cmd=$(head -c 4200)
exec </dev/null
/bin/bash --noprofile --norc -c "$cmd" 2>&1
rc=$?
printf '\n%s:%d\n' "$marker" "$rc"
WRAPPER

	write_if_changed "$UNITDIR/topcoat-readshell.socket" 0644 <<EOF
[Unit]
Description=sloppy-topcoat read-only shell (socket; only the node connects)

[Socket]
ListenStream=$SOCK
SocketUser=root
SocketGroup=$NODE_GROUP
SocketMode=0660
Accept=yes
MaxConnections=4

[Install]
WantedBy=sockets.target
EOF

	write_if_changed "$UNITDIR/topcoat-readshell@.service" 0644 <<EOF
[Unit]
Description=sloppy-topcoat read-only shell (one command)

[Service]
User=$RO_USER
Group=$RO_USER
SupplementaryGroups=$READ_GROUPS
StandardInput=socket
StandardOutput=socket
StandardError=socket
ExecStart=$LIB/run
WorkingDirectory=/
UMask=0077
Environment=LC_ALL=C.UTF-8 HOME=/nonexistent PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
# limits: the node asks for at most 60 s; this ends what still runs
RuntimeMaxSec=70
TimeoutStopSec=2
KillMode=control-group
MemoryMax=256M
TasksMax=64
CPUQuota=50%
# sandbox: no privileges, no network, nothing writable, no other people's data
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=yes
PrivateDevices=yes
PrivateNetwork=no
RestrictAddressFamilies=AF_UNIX AF_NETLINK
RestrictNamespaces=yes
RestrictRealtime=yes
LockPersonality=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
InaccessiblePaths=-/root -/home -/etc/topcoat-node -/var/lib/topcoat-node -/etc/ssl/private -/etc/shadow -/etc/shadow- -/etc/gshadow -/etc/gshadow- -/etc/sudoers -/etc/sudoers.d -/etc/ssh/ssh_host_rsa_key -/etc/ssh/ssh_host_ecdsa_key -/etc/ssh/ssh_host_ed25519_key -/etc/ssh/ssh_host_dsa_key
EOF

	mkdir -p "$UNITDIR/topcoat-readshell@.service.d"
	if [ -n "$HIDE_PATHS" ]; then
		# systemd refuses to start the unit when a listed path does not exist, unless the path
		# starts with "-". A directory that is missing on one server must not stop the shell.
		hide=""
		for p in $HIDE_PATHS; do
			case $p in -*) ;; *) p="-$p" ;; esac
			hide="$hide $p"
		done
		HIDE_PATHS=${hide# }
		write_if_changed "$UNITDIR/topcoat-readshell@.service.d/hide.conf" 0644 <<EOF
[Service]
InaccessiblePaths=$HIDE_PATHS
EOF
	else
		if [ -f "$UNITDIR/topcoat-readshell@.service.d/hide.conf" ]; then
			rm -f "$UNITDIR/topcoat-readshell@.service.d/hide.conf"
			echo "removed hide.conf (HIDE_PATHS is empty)"
			CHANGED=1
		fi
	fi

	systemctl daemon-reload
	systemctl enable --now topcoat-readshell.socket >/dev/null
	[ "$CHANGED" -eq 1 ] && systemctl restart topcoat-readshell.socket
	echo
	do_status
	cat <<EOF

Next:
  1. sudo bash $0 audit        — read the list: what the read user can still read.
  2. Fix what you do not want shown (chmod o-r FILE, or HIDE_PATHS="/path ..." and run this again).
  3. In the Topcoat GUI, node config of this server: tools.shell_socket = "$SOCK" and add
     "shell.read" to the node's capabilities. Then grant it to the users who may use it
     (admins have it already). Off until you do this.
  4. Way back: remove "shell.read" from the capabilities, then: sudo bash $0 uninstall
EOF
}

do_status() {
	id "$RO_USER" 2>&1 || true
	systemctl is-enabled topcoat-readshell.socket 2>&1 || true
	systemctl is-active topcoat-readshell.socket 2>&1 || true
	ls -l "$SOCK" 2>&1 || true
}

do_audit() {
	need_root
	id "$RO_USER" >/dev/null 2>&1 || die "$RO_USER does not exist — install first"
	echo "== who the read user is"
	id "$RO_USER"
	echo
	echo "== can it reach the Docker socket? (must say: no)"
	if runuser -u "$RO_USER" -- test -r /var/run/docker.sock -o -w /var/run/docker.sock 2>/dev/null; then
		echo "YES — fix this before use"
	else echo "no"; fi
	echo
	echo "== files named like credentials that it can read (by user permissions; the unit hides more)"
	runuser -u "$RO_USER" -- bash -c '
		timeout 120 find / -xdev \( -path /proc -o -path /sys -o -path /usr -o -path /run -o -path /dev \) -prune -o \
			-type f -readable \( -name "*.env" -o -name ".env*" -o -name "*.pem" -o -name "*.key" -o -name "id_*" \
			-o -name ".git-credentials" -o -name ".netrc" -o -name "*.kdbx" -o -name "*.pfx" -o -name "*.p12" \
			-o -name "hub.env" -o -name "connectors.json" -o -name "*secret*" -o -name "*token*" -o -name "*passw*" \
			-o -name "*credential*" \) -print 2>/dev/null | head -200' || true
	echo
	echo "== readable text files that look like they contain credentials (names only, never contents)"
	runuser -u "$RO_USER" -- bash -c '
		timeout 180 grep -rIlsE "(password|passwd|secret|token|api[_-]?key)[[:space:]]*[=:]" /etc /opt /srv /var/www /var/lib /var/log 2>/dev/null | head -200' || true
	echo
	echo "== what to do with the list"
	echo "  chmod o-r FILE              (when nothing else needs it world-readable)"
	echo "  HIDE_PATHS=\"/dir /dir2\" bash $0   (makes whole directories invisible to the shell)"
	echo "  The Topcoat secret filter masks most credentials in the output as well, but it is a"
	echo "  second line of defence, not the first."
}

do_uninstall() {
	need_root
	systemctl disable --now topcoat-readshell.socket 2>/dev/null || true
	systemctl stop 'topcoat-readshell@*.service' 2>/dev/null || true
	rm -f "$UNITDIR/topcoat-readshell.socket" "$UNITDIR/topcoat-readshell@.service"
	rm -rf "$UNITDIR/topcoat-readshell@.service.d" "$LIB"
	systemctl daemon-reload
	echo "removed units and wrapper. The user $RO_USER stays (userdel $RO_USER removes it)."
	echo "Also remove \"shell.read\" and tools.shell_socket from the node config in the GUI."
}

case "${1:-install}" in
install) do_install ;;
audit) do_audit ;;
status) do_status ;;
uninstall) do_uninstall ;;
*) die "usage: $0 [install|audit|status|uninstall]" ;;
esac
