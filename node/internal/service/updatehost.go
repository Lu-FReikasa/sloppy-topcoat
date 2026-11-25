package service

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
)

// UpdaterName is the service / unit name of the privileged updater (E2-06, D50): the same program
// as the node, started as `topcoat-node updater`, running with the rights to replace the program
// file that the node itself does not have.
const UpdaterName = Name + "-updater"

// UpdaterStateDirLinux is where the updater keeps its heartbeat, marker and result on Linux: owned
// by root, readable by the node's account.
const UpdaterStateDirLinux = "/var/lib/" + UpdaterName

// fileHost is update.Host on top of two service operations and plain file moves; the Linux and
// Windows variants only differ in how the node service is stopped and started.
type fileHost struct {
	program string // the installed program file
	stop    func(ctx context.Context) error
	start   func(ctx context.Context) error
}

func (h *fileHost) StopNode(ctx context.Context) error  { return h.stop(ctx) }
func (h *fileHost) StartNode(ctx context.Context) error { return h.start(ctx) }

// ReplaceProgram writes data next to the program file, moves the old file to <program>.prev and the
// new one into place. A leftover .prev of an older update is removed first, so RestoreProgram can
// never bring back anything but the file this call replaced.
func (h *fileHost) ReplaceProgram(_ context.Context, data []byte) error {
	next, prev := h.program+".new", h.program+".prev"
	if err := os.WriteFile(next, data, 0o755); err != nil { //nolint:gosec // an executable
		return err
	}
	_ = os.Remove(prev)
	if _, err := os.Lstat(h.program); err == nil {
		if err := os.Rename(h.program, prev); err != nil {
			_ = os.Remove(next)
			return fmt.Errorf("keep the old %s: %w", filepath.Base(h.program), err)
		}
	}
	if err := os.Rename(next, h.program); err != nil {
		_ = os.Rename(prev, h.program)
		_ = os.Remove(next)
		return err
	}
	return nil
}

// RestoreProgram puts the kept file back; without one it does nothing.
func (h *fileHost) RestoreProgram(context.Context) error {
	prev := h.program + ".prev"
	if _, err := os.Lstat(prev); err != nil {
		return nil
	}
	return os.Rename(prev, h.program)
}
