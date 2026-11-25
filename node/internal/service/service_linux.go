package service

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"

	"topcoat/node/internal/update"
)

// linuxSys holds the system operations (injectable for tests).
type linuxSys struct {
	run         func(name string, args ...string) error
	unitPath    string
	updaterUnit string
	stateDir    string
	binPath     string
	euid        func() int
	self        func() (string, error)
	lookupUser  func(string) (*user.User, error)
	lookupGroup func(string) (*user.Group, error)
	chown       func(string, int, int) error
}

func realSys() linuxSys {
	return linuxSys{
		run: func(name string, args ...string) error {
			out, err := exec.Command(name, args...).CombinedOutput() //nolint:gosec // fixed tools (useradd, systemctl)
			if err != nil {
				return fmt.Errorf("%s %s: %w: %s", name, strings.Join(args, " "), err, strings.TrimSpace(string(out)))
			}
			return nil
		},
		unitPath:    "/etc/systemd/system/" + Name + ".service",
		updaterUnit: "/etc/systemd/system/" + UpdaterName + ".service",
		stateDir:    UpdaterStateDirLinux,
		binPath:     "/usr/local/bin/" + Name,
		euid:        os.Geteuid,
		self:        os.Executable,
		lookupUser:  user.Lookup,
		lookupGroup: user.LookupGroup,
		chown:       os.Lchown,
	}
}

// UnitFile renders the systemd unit. The agent runs as its own system user;
// systemd restarts it on failure and kills its llama-server children with it.
func UnitFile(binPath, dataDir, account, stateDir string) string {
	return fmt.Sprintf(`[Unit]
Description=sloppy-topcoat node agent
Documentation=https://github.com/Lu-FReikasa/sloppy-topcoat/blob/main/docs/INSTALL-NODE.md
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=%[3]s
Group=%[3]s
ExecStart=%[1]s run --data-dir %[2]s%[4]s
Restart=on-failure
RestartSec=5
# llama-server children are stopped together with the agent.
KillMode=control-group
TimeoutStopSec=30
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ReadWritePaths=%[2]s

[Install]
WantedBy=multi-user.target
`, binPath, dataDir, account, stateFlag(stateDir))
}

// UpdaterUnitFile renders the unit of the privileged updater (E2-06). It runs as root because it
// replaces the program file, which the node account cannot write; its only inputs are the staged
// bundle it verifies against the keys compiled into the program, so the node account cannot make
// it install anything that is not signed. Restart=always: after a successful update it ends on
// purpose and systemd starts the new program.
func UpdaterUnitFile(binPath, dataDir, stateDir string) string {
	return fmt.Sprintf(`[Unit]
Description=sloppy-topcoat node updater (applies signed updates)
Documentation=https://github.com/Lu-FReikasa/sloppy-topcoat/blob/main/docs/INSTALL-NODE.md
After=network-online.target

[Service]
Type=simple
ExecStart=%[1]s updater --data-dir %[2]s%[4]s
Restart=always
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=%[2]s %[3]s %[5]s

[Install]
WantedBy=multi-user.target
`, binPath, dataDir, filepath.Dir(binPath), stateFlag(stateDir), stateDir)
}

// Install registers and starts the systemd unit. UNVERIFIED on real hardware.
func Install(o InstallOptions) error { return installLinux(realSys(), o) }

// Restart restarts the systemd unit (needs root). UNVERIFIED on real hardware.
func Restart(o RestartOptions) error { return restartLinux(realSys(), o) }

func restartLinux(s linuxSys, o RestartOptions) error {
	if _, err := os.Stat(s.unitPath); err != nil {
		return ErrNotInstalled
	}
	if s.euid() != 0 {
		return errors.New("restarting the service needs root: run it with sudo")
	}
	if err := s.run("systemctl", "restart", Name); err != nil {
		return err
	}
	fmt.Fprintf(out(o.Out), "restarted %s\n", Name)
	return nil
}

// Uninstall stops and removes the unit (and with Purge the data dir and user).
func Uninstall(o UninstallOptions) error { return uninstallLinux(realSys(), o) }

func out(w io.Writer) io.Writer {
	if w == nil {
		return io.Discard
	}
	return w
}

func installLinux(s linuxSys, o InstallOptions) error {
	w := out(o.Out)
	if s.euid() != 0 {
		return errors.New("install needs root: run it with sudo")
	}
	if o.DataDir == "" {
		return errors.New("data dir is empty")
	}
	// 1. The service account. Its own system user (with access to the GPU device nodes) unless an
	// existing account was named: that one is never created and its groups are not touched.
	account := Name
	if o.Account != "" {
		account = o.Account
	}
	u, err := s.lookupUser(account)
	if err != nil && account != Name {
		return fmt.Errorf("the account %q does not exist: create it first (the installer only creates %s)", account, Name)
	}
	if err != nil {
		fmt.Fprintf(w, "creating system user %s\n", Name)
		if err := s.run("useradd", "--system", "--home-dir", o.DataDir, "--no-create-home",
			"--shell", "/usr/sbin/nologin", Name); err != nil {
			return err
		}
		if u, err = s.lookupUser(Name); err != nil {
			return err
		}
	}
	var groups []string
	for _, g := range []string{"video", "render"} {
		if _, err := s.lookupGroup(g); err == nil && account == Name {
			groups = append(groups, g)
		}
	}
	if len(groups) > 0 {
		if err := s.run("usermod", "-aG", strings.Join(groups, ","), Name); err != nil {
			return err
		}
	}
	// 2. Binary in a fixed place.
	self, err := s.self()
	if err != nil {
		return err
	}
	if filepath.Clean(self) != s.binPath {
		fmt.Fprintf(w, "copying %s to %s\n", self, s.binPath)
		if err := copyFile(self, s.binPath, 0o755); err != nil {
			return err
		}
	}
	// 3. Data dir + pairing, owned by the service user only.
	if err := os.MkdirAll(o.DataDir, 0o700); err != nil {
		return err
	}
	if o.Pair != nil {
		if err := o.Pair(o.DataDir); err != nil {
			return err
		}
	}
	// The updater's own directory: root writes, the node account reads (a directory the node can
	// write into must never be one that root writes files into).
	if err := os.MkdirAll(s.stateDir, 0o755); err != nil { //nolint:gosec // readable by the node on purpose
		return err
	}
	if err := os.Chmod(s.stateDir, 0o755); err != nil { //nolint:gosec // see above
		return err
	}
	// The update directory belongs to the node account (it stages bundles there); the updater
	// runs as root and must not be the one that creates it.
	if err := update.NewLayout(o.DataDir).EnsureDir(); err != nil {
		return err
	}
	uid, _ := strconv.Atoi(u.Uid)
	gid, _ := strconv.Atoi(u.Gid)
	if err := chownTree(s, o.DataDir, uid, gid); err != nil {
		return err
	}
	if err := os.Chmod(o.DataDir, 0o700); err != nil { //nolint:gosec // a directory needs the x bit
		return err
	}
	// 4. Unit.
	if err := os.WriteFile(s.unitPath, []byte(UnitFile(s.binPath, o.DataDir, account, s.stateDir)), 0o644); err != nil { //nolint:gosec // systemd units are world-readable
		return err
	}
	if err := os.WriteFile(s.updaterUnit, []byte(UpdaterUnitFile(s.binPath, o.DataDir, s.stateDir)), 0o644); err != nil { //nolint:gosec // systemd units are world-readable
		return err
	}
	if err := s.run("systemctl", "daemon-reload"); err != nil {
		return err
	}
	for _, unit := range []string{Name, UpdaterName} {
		args := []string{"enable", unit}
		if !o.NoStart {
			args = []string{"enable", "--now", unit}
		}
		if err := s.run("systemctl", args...); err != nil {
			return err
		}
		if !o.NoStart {
			// Pick up a new binary / unit if the service was already running.
			if err := s.run("systemctl", "restart", unit); err != nil {
				return err
			}
		}
	}
	fmt.Fprintf(w, "installed %s (unit %s, data dir %s)\nlogs: journalctl -u %s -f\n", Name, s.unitPath, o.DataDir, Name)
	linuxFirewallNote(w)
	return nil
}

func chownTree(s linuxSys, root string, uid, gid int) error {
	return filepath.WalkDir(root, func(p string, _ fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		return s.chown(p, uid, gid)
	})
}

func uninstallLinux(s linuxSys, o UninstallOptions) error {
	w := out(o.Out)
	if s.euid() != 0 {
		return errors.New("uninstall needs root: run it with sudo")
	}
	if _, err := os.Stat(s.updaterUnit); err == nil {
		_ = s.run("systemctl", "disable", "--now", UpdaterName)
		if err := os.Remove(s.updaterUnit); err != nil {
			return err
		}
		fmt.Fprintf(w, "removed %s\n", s.updaterUnit)
	}
	if _, err := os.Stat(s.unitPath); err == nil {
		_ = s.run("systemctl", "disable", "--now", Name)
		if err := os.Remove(s.unitPath); err != nil {
			return err
		}
		_ = s.run("systemctl", "daemon-reload")
		fmt.Fprintf(w, "removed %s\n", s.unitPath)
	}
	if o.Purge {
		if o.DataDir != "" {
			if err := os.RemoveAll(o.DataDir); err != nil {
				return err
			}
			fmt.Fprintf(w, "deleted %s\n", o.DataDir)
		}
		_ = os.RemoveAll(s.stateDir)
		if _, err := s.lookupUser(Name); err == nil {
			_ = s.run("userdel", Name)
		}
		if err := os.Remove(s.binPath); err == nil {
			fmt.Fprintf(w, "deleted %s\n", s.binPath)
		}
	}
	return nil
}

func copyFile(src, dst string, mode os.FileMode) error {
	b, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil { //nolint:gosec // /usr/local/bin
		return err
	}
	tmp := dst + ".new"
	if err := os.WriteFile(tmp, b, mode); err != nil { //nolint:gosec // fixed destination (/usr/local/bin/topcoat-node)
		return err
	}
	return os.Rename(tmp, dst) // atomic even while the old binary runs
}

// EnsureUpdaterRestarts is a Windows matter (systemd restarts the updater by its unit's Restart=).
func EnsureUpdaterRestarts() error { return nil }

// IsWindowsService is false on Linux.
func IsWindowsService() bool { return false }

// RunWindowsService is only used on Windows.
func RunWindowsService(string, func(stop <-chan struct{}) int) int { return 1 }

// NewHost is the updater's view of this machine: systemctl for the node unit, plain file moves for
// the program. UNVERIFIED on real hardware.
func NewHost() update.Host { return newLinuxHost(realSys()) }

func newLinuxHost(s linuxSys) update.Host {
	return &fileHost{
		program: s.binPath,
		stop:    func(context.Context) error { return s.run("systemctl", "stop", Name) },
		start:   func(context.Context) error { return s.run("systemctl", "start", Name) },
	}
}

// stateFlag is the command line part that hands the updater's directory to node and updater.
func stateFlag(stateDir string) string {
	if stateDir == "" {
		return ""
	}
	return " --update-state-dir " + stateDir
}
