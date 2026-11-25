package service

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"
)

type fakeOps struct {
	calls []string
	procs []exeProcess
	alive map[uint32]bool
	ops   exeOps
}

func newFakeOps(self uint32, procs ...exeProcess) *fakeOps {
	f := &fakeOps{procs: procs, alive: map[uint32]bool{}}
	for _, p := range procs {
		f.alive[p.PID] = true
	}
	f.ops = exeOps{
		endTask: func() { f.calls = append(f.calls, "end-task") },
		list:    func() ([]exeProcess, error) { f.calls = append(f.calls, "list"); return f.procs, nil },
		kill: func(pid uint32) error {
			f.calls = append(f.calls, "kill "+itoa(pid))
			f.alive[pid] = false
			return nil
		},
		waitGone: func(pid uint32, _ time.Duration) bool {
			f.calls = append(f.calls, "wait "+itoa(pid))
			return !f.alive[pid]
		},
		selfPID:   self,
		waitLimit: time.Second,
	}
	return f
}

func itoa(n uint32) string { return strconv.FormatUint(uint64(n), 10) }

func TestInstallEndsTheTaskAndEveryOtherNodeProcessButNotItself(t *testing.T) {
	f := newFakeOps(100,
		exeProcess{PID: 4, Name: "System"},
		exeProcess{PID: 100, Name: "topcoat-node.exe"}, // the installer
		exeProcess{PID: 200, Name: "TopCoat-Node.EXE"}, // the input helper in the user's session
		exeProcess{PID: 300, Name: "topcoat-node.exe"}, // a node started from a shell
		exeProcess{PID: 400, Name: "llama-server.exe"},
	)
	var out bytes.Buffer
	if err := freeExecutable("topcoat-node.exe", f.ops, &out); err != nil {
		t.Fatal(err)
	}
	want := []string{"end-task", "list", "kill 200", "wait 200", "kill 300", "wait 300"}
	if !reflect.DeepEqual(f.calls, want) {
		t.Fatalf("calls %v, want %v (the task first, then the others, each waited for)", f.calls, want)
	}
	if f.alive[100] != true || f.alive[200] || f.alive[300] || !f.alive[400] || !f.alive[4] {
		t.Fatalf("alive %v: only the other node processes may be ended", f.alive)
	}
	if !strings.Contains(out.String(), "pid 200") || !strings.Contains(out.String(), "pid 300") || strings.Contains(out.String(), "pid 100") {
		t.Fatalf("%s", out.String())
	}
}

func TestInstallSaysWhichProcessSurvived(t *testing.T) {
	f := newFakeOps(1, exeProcess{PID: 7, Name: "topcoat-node.exe"}, exeProcess{PID: 8, Name: "topcoat-node.exe"})
	f.ops.kill = func(pid uint32) error {
		if pid == 7 {
			return errors.New("Zugriff verweigert")
		}
		f.alive[pid] = false
		return nil
	}
	f.alive[8] = true
	f.ops.waitGone = func(uint32, time.Duration) bool { return false }
	err := freeExecutable("topcoat-node.exe", f.ops, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "pid 7: Zugriff verweigert") || !strings.Contains(err.Error(), "pid 8 did not end") {
		t.Fatalf("%v", err)
	}
}

func TestListFailureIsAnError(t *testing.T) {
	ops := exeOps{endTask: func() {}, list: func() ([]exeProcess, error) { return nil, errors.New("boom") }}
	if err := freeExecutable("topcoat-node.exe", ops, &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "boom") {
		t.Fatalf("%v", err)
	}
}

func TestReplaceFileMovesTheOldOneAsideAndLeavesNoLitter(t *testing.T) {
	dir := t.TempDir()
	src, dst := filepath.Join(dir, "new.exe"), filepath.Join(dir, "prog", "topcoat-node.exe")
	write := func(p, s string) {
		t.Helper()
		if err := os.MkdirAll(filepath.Dir(p), 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(s), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write(src, "v2")
	write(dst, "v1")
	write(dst+".old", "v0") // litter of the update before
	if err := replaceFile(src, dst); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(dst); string(b) != "v2" {
		t.Fatalf("dst = %q", b)
	}
	entries, _ := os.ReadDir(filepath.Dir(dst))
	var names []string
	for _, e := range entries {
		names = append(names, e.Name())
	}
	if !reflect.DeepEqual(names, []string{"topcoat-node.exe"}) {
		t.Fatalf("left behind: %v", names)
	}
	// a first install has nothing to move aside
	dst2 := filepath.Join(dir, "fresh", "topcoat-node.exe")
	if err := replaceFile(src, dst2); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(dst2); string(b) != "v2" {
		t.Fatalf("dst2 = %q", b)
	}
}
