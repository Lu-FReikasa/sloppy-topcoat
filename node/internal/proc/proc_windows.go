package proc

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// UNVERIFIED on real hardware (job objects, memory counters).

type sysState struct{}

// gone reports whether a failed kill only means that the process is already finished or
// already being torn down. Windows answers TerminateProcess for a process that is
// terminating (killed a moment ago, its memory still being released) with ERROR_ACCESS_DENIED
// — seen as "unload all: TerminateProcess: Zugriff verweigert" when a 30B model was stopped.
func gone(err error) bool {
	return errors.Is(err, os.ErrProcessDone) || errors.Is(err, windows.ERROR_ACCESS_DENIED)
}

// job is a Job object with KILL_ON_JOB_CLOSE: when the agent exits (even when
// it crashes) Windows closes the handle and kills every llama-server child.
var job = func() windows.Handle {
	h, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return 0
	}
	info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
	info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if _, err := windows.SetInformationJobObject(h, windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
		_ = windows.CloseHandle(h)
		return 0
	}
	return h
}()

// prepare hides the window and, with low, starts the process in the IDLE priority class
// (D24 step 1): it then only gets CPU time nobody else wants.
func prepare(cmd *exec.Cmd, low bool) {
	flags := uint32(windows.CREATE_NO_WINDOW | windows.CREATE_NEW_PROCESS_GROUP)
	if low {
		flags |= windows.IDLE_PRIORITY_CLASS
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: flags}
}

var (
	ntdll              = windows.NewLazySystemDLL("ntdll.dll")
	procNtSuspendProc  = ntdll.NewProc("NtSuspendProcess")
	procNtResumeProc   = ntdll.NewProc("NtResumeProcess")
	errNtStatusFailure = errors.New("the process could not be suspended / resumed")
)

// suspend freezes every thread of the process (NtSuspendProcess). UNVERIFIED on real hardware.
func suspend(pid int) error { return ntSuspendResume(pid, procNtSuspendProc) }

// resume continues a suspended process (NtResumeProcess). UNVERIFIED on real hardware.
func resume(pid int) error { return ntSuspendResume(pid, procNtResumeProc) }

func ntSuspendResume(pid int, proc *windows.LazyProc) error {
	h, err := windows.OpenProcess(windows.PROCESS_SUSPEND_RESUME, false, uint32(pid))
	if err != nil {
		return err
	}
	defer windows.CloseHandle(h) //nolint:errcheck // best effort
	if status, _, _ := proc.Call(uintptr(h)); status != 0 {
		return fmt.Errorf("%w (NTSTATUS 0x%x)", errNtStatusFailure, status)
	}
	return nil
}

func afterStart(cmd *exec.Cmd, _ bool) sysState {
	if job == 0 {
		return sysState{}
	}
	h, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(cmd.Process.Pid))
	if err != nil {
		return sysState{}
	}
	defer windows.CloseHandle(h) //nolint:errcheck // best effort
	_ = windows.AssignProcessToJobObject(job, h)
	return sysState{}
}

// terminate kills: console-less children cannot receive CTRL_BREAK, and
// llama-server holds no state that needs a clean shutdown.
func terminate(cmd *exec.Cmd) error { return cmd.Process.Kill() }

type processMemoryCounters struct {
	CB                         uint32
	PageFaultCount             uint32
	PeakWorkingSetSize         uintptr
	WorkingSetSize             uintptr
	QuotaPeakPagedPoolUsage    uintptr
	QuotaPagedPoolUsage        uintptr
	QuotaPeakNonPagedPoolUsage uintptr
	QuotaNonPagedPoolUsage     uintptr
	PagefileUsage              uintptr
	PeakPagefileUsage          uintptr
}

var procGetProcessMemoryInfo = windows.NewLazySystemDLL("kernel32.dll").NewProc("K32GetProcessMemoryInfo")

func rssMB(pid int) int64 {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return 0
	}
	defer windows.CloseHandle(h) //nolint:errcheck // best effort
	var c processMemoryCounters
	c.CB = uint32(unsafe.Sizeof(c))
	r, _, _ := procGetProcessMemoryInfo.Call(uintptr(h), uintptr(unsafe.Pointer(&c)), uintptr(c.CB))
	if r == 0 {
		return 0
	}
	return int64(c.WorkingSetSize / (1 << 20))
}
