package service

import (
	"bytes"
	"errors"
	"os"
	"os/user"
	"path/filepath"
	"strings"
	"testing"
)

func fakeSys(t *testing.T, exists bool) (linuxSys, *[]string, string) {
	t.Helper()
	root := t.TempDir()
	self := filepath.Join(root, "download", "topcoat-node-linux-amd64")
	_ = os.MkdirAll(filepath.Dir(self), 0o755)
	_ = os.WriteFile(self, []byte("ELF"), 0o755)
	var calls []string
	created := exists
	s := linuxSys{
		run: func(name string, args ...string) error {
			calls = append(calls, name+" "+strings.Join(args, " "))
			if name == "useradd" {
				created = true
			}
			return nil
		},
		unitPath:    filepath.Join(root, "topcoat-node.service"),
		updaterUnit: filepath.Join(root, "topcoat-node-updater.service"),
		stateDir:    filepath.Join(root, "state"),
		binPath:     filepath.Join(root, "bin", "topcoat-node"),
		euid:        func() int { return 0 },
		self:        func() (string, error) { return self, nil },
		lookupUser: func(string) (*user.User, error) {
			if !created {
				return nil, errors.New("unknown user")
			}
			return &user.User{Uid: "990", Gid: "990"}, nil
		},
		lookupGroup: func(g string) (*user.Group, error) {
			if g == "render" {
				return &user.Group{}, nil
			}
			return nil, errors.New("no group")
		},
		chown: func(string, int, int) error { return nil },
	}
	return s, &calls, root
}

func TestInstallLinux(t *testing.T) {
	s, calls, root := fakeSys(t, false)
	data := filepath.Join(root, "var", "lib", "topcoat-node")
	paired := ""
	var buf bytes.Buffer
	err := installLinux(s, InstallOptions{DataDir: data, Out: &buf, Pair: func(d string) error { paired = d; return nil }})
	if err != nil {
		t.Fatal(err)
	}
	if paired != data {
		t.Fatalf("paired into %q", paired)
	}
	want := []string{
		"useradd --system --home-dir " + data + " --no-create-home --shell /usr/sbin/nologin topcoat-node",
		"usermod -aG render topcoat-node",
		"systemctl daemon-reload",
		"systemctl enable --now topcoat-node",
		"systemctl restart topcoat-node",
		"systemctl enable --now topcoat-node-updater",
		"systemctl restart topcoat-node-updater",
	}
	if strings.Join(*calls, "\n") != strings.Join(want, "\n") {
		t.Fatalf("calls:\n%s", strings.Join(*calls, "\n"))
	}
	unit, _ := os.ReadFile(s.unitPath)
	for _, line := range []string{"User=topcoat-node", "ExecStart=" + s.binPath + " run --data-dir " + data, "Restart=on-failure", "ReadWritePaths=" + data, "WantedBy=multi-user.target"} {
		if !strings.Contains(string(unit), line) {
			t.Errorf("unit lacks %q", line)
		}
	}
	if b, err := os.ReadFile(s.binPath); err != nil || string(b) != "ELF" {
		t.Fatal("binary not copied")
	}
	if st, _ := os.Stat(data); st.Mode().Perm() != 0o700 {
		t.Fatalf("data dir mode %v", st.Mode())
	}
}

func TestInstallLinuxCanRunAsAnExistingAccountAndNeverCreatesOne(t *testing.T) {
	s, calls, root := fakeSys(t, true)
	data := filepath.Join(root, "srv", "node-data")
	if err := installLinux(s, InstallOptions{DataDir: data, Account: "topcoat-dev"}); err != nil {
		t.Fatal(err)
	}
	unit, _ := os.ReadFile(s.unitPath)
	if !strings.Contains(string(unit), "User=topcoat-dev") || !strings.Contains(string(unit), "Group=topcoat-dev") {
		t.Fatalf("the unit must run as the named account:\n%s", unit)
	}
	for _, c := range *calls {
		if strings.HasPrefix(c, "useradd") || strings.HasPrefix(c, "usermod") {
			t.Fatalf("an existing account is neither created nor changed: %s", c)
		}
	}
	// an account that does not exist is an error, not a new user
	s2, calls2, root2 := fakeSys(t, false)
	err := installLinux(s2, InstallOptions{DataDir: filepath.Join(root2, "d"), Account: "ghost"})
	if err == nil || !strings.Contains(err.Error(), "does not exist") {
		t.Fatalf("want an error about the missing account, got %v", err)
	}
	for _, c := range *calls2 {
		if strings.HasPrefix(c, "useradd") {
			t.Fatalf("no user may be created for a named account: %s", c)
		}
	}
}

func TestInstallLinuxNeedsRootAndPairErrors(t *testing.T) {
	s, _, root := fakeSys(t, true)
	s.euid = func() int { return 1000 }
	if err := installLinux(s, InstallOptions{DataDir: root}); err == nil || !strings.Contains(err.Error(), "sudo") {
		t.Fatalf("got %v", err)
	}
	s.euid = func() int { return 0 }
	err := installLinux(s, InstallOptions{DataDir: filepath.Join(root, "d"), NoStart: true, Pair: func(string) error { return errors.New("code_expired") }})
	if err == nil || !strings.Contains(err.Error(), "code_expired") {
		t.Fatalf("got %v", err)
	}
	if _, err := os.Stat(s.unitPath); !os.IsNotExist(err) {
		t.Fatal("unit written despite pairing failure")
	}
}

func TestUninstallLinuxPurge(t *testing.T) {
	s, calls, root := fakeSys(t, true)
	data := filepath.Join(root, "data")
	_ = os.MkdirAll(data, 0o700)
	_ = os.WriteFile(s.unitPath, []byte("x"), 0o644)
	_ = os.MkdirAll(filepath.Dir(s.binPath), 0o755)
	_ = os.WriteFile(s.binPath, []byte("x"), 0o755)
	if err := uninstallLinux(s, UninstallOptions{DataDir: data, Purge: true}); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{s.unitPath, data, s.binPath} {
		if _, err := os.Stat(p); !os.IsNotExist(err) {
			t.Errorf("%s still exists", p)
		}
	}
	if !strings.Contains(strings.Join(*calls, ";"), "systemctl disable --now topcoat-node") || !strings.Contains(strings.Join(*calls, ";"), "userdel topcoat-node") {
		t.Fatalf("%v", *calls)
	}
}

func TestRestartLinux(t *testing.T) {
	s, calls, root := fakeSys(t, true)
	if err := restartLinux(s, RestartOptions{}); !errors.Is(err, ErrNotInstalled) {
		t.Fatalf("no unit: %v", err)
	}
	_ = os.MkdirAll(filepath.Dir(s.unitPath), 0o755)
	_ = os.WriteFile(s.unitPath, []byte("x"), 0o644)
	s.euid = func() int { return 1000 }
	if err := restartLinux(s, RestartOptions{}); err == nil || !strings.Contains(err.Error(), "sudo") {
		t.Fatalf("needs root: %v", err)
	}
	s.euid = func() int { return 0 }
	if err := restartLinux(s, RestartOptions{}); err != nil {
		t.Fatal(err)
	}
	if strings.Join(*calls, "\n") != "systemctl restart topcoat-node" {
		t.Fatalf("calls %v (root %s)", *calls, root)
	}
}

func TestUpdaterUnitRunsAsRootAndRestartsAlways(t *testing.T) {
	u := UpdaterUnitFile("/usr/local/bin/topcoat-node", "/var/lib/topcoat-node", "/var/lib/topcoat-node-updater")
	for _, want := range []string{
		"ExecStart=/usr/local/bin/topcoat-node updater --data-dir /var/lib/topcoat-node --update-state-dir /var/lib/topcoat-node-updater",
		"Restart=always", "NoNewPrivileges=yes", "ProtectSystem=strict",
		"ReadWritePaths=/var/lib/topcoat-node /usr/local/bin /var/lib/topcoat-node-updater",
	} {
		if !strings.Contains(u, want) {
			t.Errorf("updater unit lacks %q:\n%s", want, u)
		}
	}
	if strings.Contains(u, "User=") {
		t.Error("the updater must run as root")
	}
}

func TestInstallLinuxCreatesTheUpdateDirForTheNodeAccount(t *testing.T) {
	s, _, root := fakeSys(t, false)
	data := filepath.Join(root, "data")
	var chowned []string
	s.chown = func(p string, _, _ int) error { chowned = append(chowned, p); return nil }
	if err := installLinux(s, InstallOptions{DataDir: data, NoStart: true}); err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(data, "update")
	if fi, err := os.Stat(dir); err != nil || !fi.IsDir() {
		t.Fatalf("update dir: %v", err)
	}
	found := false
	for _, p := range chowned {
		found = found || p == dir
	}
	if !found {
		t.Fatalf("%s was not handed to the node account: %v", dir, chowned)
	}
}

func TestInstallLinuxGivesTheUpdaterItsOwnStateDirectory(t *testing.T) {
	s, _, root := fakeSys(t, false)
	data := filepath.Join(root, "data")
	if err := installLinux(s, InstallOptions{DataDir: data, NoStart: true}); err != nil {
		t.Fatal(err)
	}
	fi, err := os.Stat(s.stateDir)
	if err != nil || !fi.IsDir() || fi.Mode().Perm() != 0o755 {
		t.Fatalf("state dir: %v %v", fi, err)
	}
	flag := "--update-state-dir " + s.stateDir
	for _, unit := range []string{s.unitPath, s.updaterUnit} {
		b, _ := os.ReadFile(unit)
		if !strings.Contains(string(b), flag) {
			t.Errorf("%s lacks %q:\n%s", unit, flag, b)
		}
	}
	// the state directory is not below the node's data directory: the node cannot swap it
	if strings.HasPrefix(s.stateDir, data) {
		t.Fatal("state dir inside the data dir")
	}
}
