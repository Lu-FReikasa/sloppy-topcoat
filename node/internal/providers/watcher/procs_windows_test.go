package watcher

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"topcoat/node/internal/winproc"
)

func TestListProcessesFindsSelf(t *testing.T) {
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	procs, err := listProcesses()
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range procs {
		if strings.EqualFold(p.Path, self) && strings.EqualFold(p.Name, filepath.Base(self)) {
			return
		}
	}
	t.Fatalf("own process %s not in %d processes", self, len(procs))
}

// The NT path of the own process converts to the same path os.Executable reports.
func TestNTImagePathOfSelf(t *testing.T) {
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	nt := winproc.NTImagePath(uint32(os.Getpid()), make([]uint16, 32767))
	if nt == "" {
		t.Fatal("NtQuerySystemInformation returned no path")
	}
	if got := winproc.NTToDosPath(nt, winproc.DosDrives()); !strings.EqualFold(got, self) {
		t.Fatalf("nt %q -> %q, want %q", nt, got, self)
	}
}
