package watcher

import (
	"os"
	"path/filepath"
	"testing"
)

func TestListProcFixture(t *testing.T) {
	if _, err := os.Stat("/proc/self/exe"); err != nil {
		t.Skip("no /proc")
	}
	root := t.TempDir()
	mk := func(pid, comm, exe string) {
		d := filepath.Join(root, pid)
		_ = os.MkdirAll(d, 0o755)
		_ = os.WriteFile(filepath.Join(d, "comm"), []byte(comm+"\n"), 0o644)
		if exe != "" {
			_ = os.Symlink(exe, filepath.Join(d, "exe"))
		}
	}
	mk("1", "init", "/sbin/init")
	mk("42", "wine-game", "")
	_ = os.MkdirAll(filepath.Join(root, "self"), 0o755)
	procs, err := listProc(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(procs) != 2 {
		t.Fatalf("%+v", procs)
	}
	names := map[string]string{}
	for _, p := range procs {
		names[p.Name] = p.Path
	}
	if names["init"] != "/sbin/init" || names["wine-game"] != "" {
		t.Fatalf("%+v", names)
	}
}
