package service

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// This file is plain Go (tested on every OS); the Windows glue that lists and ends processes sits
// in service_windows.go.

// exeProcess is one running program.
type exeProcess struct {
	PID  uint32
	Name string // base name of the program file, e.g. topcoat-node.exe
}

// exeOps are the operating system calls freeExecutable needs.
type exeOps struct {
	endTask   func()                                 // schtasks /End of the input helper task
	list      func() ([]exeProcess, error)           // all running programs
	kill      func(pid uint32) error                 // end one
	waitGone  func(pid uint32, d time.Duration) bool // true once the process has ended
	selfPID   uint32
	waitLimit time.Duration
}

// freeExecutable makes the program file replaceable: a Windows program file cannot be replaced
// while any process runs from it, and the node has more of them than the service — the input
// helper (same file, started by the task scheduler in the user's session) and possibly a node
// started from a shell. So: end the task, then end every other process of that program, and wait
// until they are gone. It returns an error naming what survived. The calling process is never
// touched.
func freeExecutable(name string, ops exeOps, w io.Writer) error {
	ops.endTask()
	procs, err := ops.list()
	if err != nil {
		return fmt.Errorf("list the running programs: %w", err)
	}
	var stuck []string
	for _, p := range procs {
		if p.PID == ops.selfPID || !strings.EqualFold(p.Name, name) {
			continue
		}
		fmt.Fprintf(w, "ending %s (pid %d)\n", p.Name, p.PID)
		if err := ops.kill(p.PID); err != nil {
			stuck = append(stuck, fmt.Sprintf("pid %d: %v", p.PID, err))
			continue
		}
		if !ops.waitGone(p.PID, ops.waitLimit) {
			stuck = append(stuck, fmt.Sprintf("pid %d did not end in %s", p.PID, ops.waitLimit))
		}
	}
	if len(stuck) > 0 {
		return fmt.Errorf("%s is still running: %s", name, strings.Join(stuck, "; "))
	}
	return nil
}

// replaceFile puts the new program file at dst. A running program can be renamed but not deleted
// or overwritten, so the old file is moved aside first (dst.old, removed at the next install when
// nothing runs from it any more) — a process that slipped through freeExecutable no longer blocks
// the update, it only keeps running the old code until it ends.
func replaceFile(src, dst string) error {
	b, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil { //nolint:gosec // %ProgramFiles% is ACL-protected by Windows
		return err
	}
	if err := os.WriteFile(dst+".new", b, 0o755); err != nil { //nolint:gosec // executable
		return err
	}
	_ = os.Remove(dst + ".old") // the leftover of the last update, if nothing runs from it any more
	aside := false
	if _, err := os.Stat(dst); err == nil {
		if err := os.Rename(dst, dst+".old"); err != nil {
			_ = os.Remove(dst + ".new")
			return fmt.Errorf("move the old %s aside: %w", filepath.Base(dst), err)
		}
		aside = true
	}
	if err := os.Rename(dst+".new", dst); err != nil {
		if aside {
			_ = os.Rename(dst+".old", dst) // put the old one back
		}
		_ = os.Remove(dst + ".new")
		return err
	}
	_ = os.Remove(dst + ".old") // fails while the old program still runs: next install
	return nil
}
