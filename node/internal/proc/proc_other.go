//go:build !linux && !windows

package proc

import (
	"errors"
	"os"
	"os/exec"
	"syscall"
)

type sysState struct{}

func gone(err error) bool { return errors.Is(err, os.ErrProcessDone) }

func prepare(*exec.Cmd, bool)             {}
func afterStart(*exec.Cmd, bool) sysState { return sysState{} }
func terminate(cmd *exec.Cmd) error       { return cmd.Process.Signal(syscall.SIGTERM) }
func rssMB(int) int64                     { return 0 }
func suspend(pid int) error               { return syscall.Kill(pid, syscall.SIGSTOP) }
func resume(pid int) error                { return syscall.Kill(pid, syscall.SIGCONT) }
