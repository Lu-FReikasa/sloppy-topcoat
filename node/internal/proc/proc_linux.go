package proc

import (
	"bufio"
	"bytes"
	"errors"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
)

type sysState struct{}

func gone(err error) bool { return errors.Is(err, os.ErrProcessDone) }

// prepare makes the kernel kill the child when the agent dies (Pdeathsig).
// Caveat: Pdeathsig fires when the *thread* that forked exits; Go only ends
// threads that were locked with LockOSThread, which the agent does not do.
func prepare(cmd *exec.Cmd, _ bool) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Pdeathsig: syscall.SIGKILL}
}

// afterStart lowers the priority of the new process (D24 step 1): nice 19 on every thread it
// has (threads it creates later inherit the value) and the idle I/O class.
func afterStart(cmd *exec.Cmd, low bool) sysState {
	if low {
		lowerPriority(cmd.Process.Pid)
	}
	return sysState{}
}

// lowerPriority sets nice 19 for the process's threads. Lowering a priority needs no privilege.
func lowerPriority(pid int) {
	tasks, err := os.ReadDir("/proc/" + strconv.Itoa(pid) + "/task")
	if err != nil {
		_ = syscall.Setpriority(syscall.PRIO_PROCESS, pid, niceLowest)
	}
	for _, t := range tasks {
		if tid, err := strconv.Atoi(t.Name()); err == nil {
			_ = syscall.Setpriority(syscall.PRIO_PROCESS, tid, niceLowest)
		}
	}
	// I/O class "idle" (ionice -c 3): model loading must not make the user's disk stutter.
	// Best effort: ionice is part of util-linux, present on every mainstream distribution.
	if bin, err := exec.LookPath("ionice"); err == nil {
		_ = exec.Command(bin, "-c", "3", "-p", strconv.Itoa(pid)).Run() //nolint:gosec // fixed tool, numeric argument
	}
}

const niceLowest = 19

func suspend(pid int) error { return syscall.Kill(pid, syscall.SIGSTOP) }
func resume(pid int) error  { return syscall.Kill(pid, syscall.SIGCONT) }

func terminate(cmd *exec.Cmd) error { return cmd.Process.Signal(syscall.SIGTERM) }

func rssMB(pid int) int64 {
	b, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/status")
	if err != nil {
		return 0
	}
	return parseVMRSS(b)
}

// parseVMRSS reads "VmRSS:   123456 kB" from /proc/<pid>/status.
func parseVMRSS(b []byte) int64 {
	sc := bufio.NewScanner(bytes.NewReader(b))
	for sc.Scan() {
		line := sc.Text()
		if !strings.HasPrefix(line, "VmRSS:") {
			continue
		}
		f := strings.Fields(line)
		if len(f) >= 2 {
			kb, _ := strconv.ParseInt(f[1], 10, 64)
			return kb / 1024
		}
	}
	return 0
}
