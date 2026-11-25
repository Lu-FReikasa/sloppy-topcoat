package service

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"

	"topcoat/node/internal/update"
)

// UNVERIFIED on real hardware: everything in this file.

var (
	netapi32                    = windows.NewLazySystemDLL("netapi32.dll")
	procNetLocalGroupAddMembers = netapi32.NewProc("NetLocalGroupAddMembers")
	procNetLocalGroupDelMembers = netapi32.NewProc("NetLocalGroupDelMembers")
)

const (
	errorMemberNotInAlias = 1377 // ERROR_MEMBER_NOT_IN_ALIAS
	errorMemberInAlias    = 1378 // ERROR_MEMBER_IN_ALIAS
	errorNoSuchMember     = 1387 // ERROR_NO_SUCH_MEMBER
)

func programDir() string {
	base := os.Getenv("ProgramFiles")
	if base == "" {
		base = `C:\Program Files`
	}
	return filepath.Join(base, Name)
}

// UpdaterStateDir is the updater's own directory (heartbeat, marker, result): written by SYSTEM,
// read by the node's account.
func UpdaterStateDir() string {
	base := os.Getenv("ProgramData")
	if base == "" {
		base = `C:\ProgramData`
	}
	return filepath.Join(base, UpdaterName)
}

func applySDDL(dir, sddl string) error {
	sd, err := windows.SecurityDescriptorFromString(sddl)
	if err != nil {
		return err
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return err
	}
	return windows.SetNamedSecurityInfo(dir, windows.SE_FILE_OBJECT,
		windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION, nil, nil, dacl, nil)
}

// SecureDir restricts dir to SYSTEM, Administrators and the node's virtual
// service account.
func SecureDir(dir string) error { return secureDir(dir, AccountSID(Name)) }

func secureDir(dir, sid string) error {
	sd, err := windows.SecurityDescriptorFromString(dataDirSDDLFor(sid))
	if err != nil {
		return err
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return err
	}
	return windows.SetNamedSecurityInfo(dir, windows.SE_FILE_OBJECT,
		windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION, nil, nil, dacl, nil)
}

// performanceMonitorUsers resolves the localized name of the builtin group
// (e.g. "Leistungsüberwachungsbenutzer" on German Windows) from its SID.
func performanceMonitorUsers() (string, error) {
	sid, err := windows.StringToSid(PerformanceMonitorUsersSID)
	if err != nil {
		return "", err
	}
	name, _, _, err := sid.LookupAccount("")
	return name, err
}

// groupMembership adds (or removes) the node's service account to the
// "Performance Monitor Users" group: without it the GPU counters cannot be
// read and no GPU is found (test 1, A7).
func groupMembership(add bool) error {
	group, err := performanceMonitorUsers()
	if err != nil {
		return fmt.Errorf("look up group %s: %w", PerformanceMonitorUsersSID, err)
	}
	sid, err := windows.StringToSid(AccountSID(Name))
	if err != nil {
		return err
	}
	g, err := windows.UTF16PtrFromString(group)
	if err != nil {
		return err
	}
	info := struct{ sid *windows.SID }{sid} // LOCALGROUP_MEMBERS_INFO_0
	proc := procNetLocalGroupAddMembers
	if !add {
		proc = procNetLocalGroupDelMembers
	}
	r, _, _ := proc.Call(0, uintptr(unsafe.Pointer(g)), 0, uintptr(unsafe.Pointer(&info)), 1)
	switch r {
	case 0, errorMemberInAlias:
		return nil
	case errorMemberNotInAlias, errorNoSuchMember:
		if !add {
			return nil
		}
	}
	return fmt.Errorf("%s %s: error %d", map[bool]string{true: "add to", false: "remove from"}[add], group, r)
}

// Install copies the binary to %ProgramFiles%\topcoat-node, pairs, registers
// an auto-start service running as the virtual account NT SERVICE\topcoat-node
// (D17) that restarts on failure, adds that account to "Performance Monitor
// Users" and gives it (plus SYSTEM and Administrators) the data dir.
func Install(o InstallOptions) error {
	w := o.Out
	if w == nil {
		w = io.Discard
	}
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect to the service manager (run in an elevated PowerShell): %w", err)
	}
	defer m.Disconnect() //nolint:errcheck // best effort
	if s, err := m.OpenService(UpdaterName); err == nil {
		// The updater runs from the program file too.
		stopService(s)
		_ = s.Delete()
		_ = s.Close()
	}
	if s, err := m.OpenService(Name); err == nil {
		fmt.Fprintln(w, "stopping and replacing the existing service")
		stopService(s)
		_ = s.Delete()
		_ = s.Close()
		time.Sleep(2 * time.Second) // let the SCM release the old entry
	}

	// A running program keeps its file locked, and the node is more than the service: the input
	// helper runs from the same file in the user's session, and a node may run from a shell.
	if err := freeExecutable(Name+".exe", windowsExeOps(), w); err != nil {
		return fmt.Errorf("the program file cannot be replaced: %w", err)
	}
	self, err := os.Executable()
	if err != nil {
		return err
	}
	exe := filepath.Join(programDir(), Name+".exe")
	if !sameFile(self, exe) {
		fmt.Fprintf(w, "copying %s to %s\n", self, exe)
		if err := replaceFile(self, exe); err != nil {
			return err
		}
	}
	if err := os.MkdirAll(o.DataDir, 0o700); err != nil {
		return err
	}
	// SYSTEM and Administrators only until the service account exists.
	if err := secureDir(o.DataDir, ""); err != nil {
		return fmt.Errorf("restrict %s: %w", o.DataDir, err)
	}
	if o.Pair != nil {
		if err := o.Pair(o.DataDir); err != nil {
			return err
		}
	}
	s, err := m.CreateService(Name, exe, mgr.Config{
		DisplayName:      "sloppy-topcoat node",
		Description:      "sloppy-topcoat node agent (manages llama.cpp models on this machine)",
		StartType:        mgr.StartAutomatic,
		DelayedAutoStart: true,
		ServiceStartName: Account,
		SidType:          windows.SERVICE_SID_TYPE_UNRESTRICTED,
	}, "run", "--data-dir", o.DataDir, "--update-state-dir", UpdaterStateDir())
	if err != nil {
		return fmt.Errorf("create service: %w", err)
	}
	defer s.Close()
	// The virtual account exists now: grant it the data dir and the counters.
	if err := SecureDir(o.DataDir); err != nil {
		return fmt.Errorf("grant %s access to %s: %w", Account, o.DataDir, err)
	}
	if err := groupMembership(true); err != nil {
		return err
	}
	// The update directory inherits the ACL just set: the node (staging) and the updater (SYSTEM).
	if err := update.NewLayout(o.DataDir).EnsureDir(); err != nil {
		return err
	}
	// The updater's own directory: SYSTEM writes, the node account can only read.
	state := UpdaterStateDir()
	if err := os.MkdirAll(state, 0o755); err != nil { //nolint:gosec // ACL set right below
		return err
	}
	if err := applySDDL(state, stateDirSDDLFor(AccountSID(Name))); err != nil {
		return fmt.Errorf("restrict %s: %w", state, err)
	}
	fmt.Fprintf(w, "service account %s (%s), member of Performance Monitor Users\n", Account, AccountSID(Name))
	if err := s.SetRecoveryActions([]mgr.RecoveryAction{
		{Type: mgr.ServiceRestart, Delay: 5 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 15 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 60 * time.Second},
	}, 24*3600); err != nil {
		return fmt.Errorf("set recovery actions: %w", err)
	}
	if !o.NoStart {
		if err := s.Start(); err != nil {
			return fmt.Errorf("start service: %w", err)
		}
	}
	if err := installUpdater(m, exe, o.DataDir, !o.NoStart, w); err != nil {
		return err
	}
	fmt.Fprintf(w, "installed service %s (data dir %s)\nlogs: %s\n", Name, o.DataDir, filepath.Join(o.DataDir, "logs", Name+".log"))
	// Direct model transfer (E2-07): best effort, a failure is only reported.
	ensureTransferFirewallRule(netsh, exe, w)
	// The input helper is a convenience for the yield logic; failing to set it up is not a
	// failed install.
	if err := installInputHelper(exe, w); err != nil {
		fmt.Fprintf(w, "could not set up the input helper (the node then cannot see keyboard/mouse input): %v\n", err)
	}
	return nil
}

// installUpdater registers the privileged updater (E2-06): LocalSystem, because it replaces the
// program file in %ProgramFiles% which the node account cannot write. After a successful update it
// ends with a non-zero code on purpose so that the recovery action starts the new program.
func installUpdater(m *mgr.Mgr, exe, dataDir string, start bool, w io.Writer) error {
	s, err := m.CreateService(UpdaterName, exe, mgr.Config{
		DisplayName: "sloppy-topcoat node updater",
		Description: "applies signed sloppy-topcoat node updates (verifies them against the keys built into the program)",
		StartType:   mgr.StartAutomatic,
	}, "updater", "--data-dir", dataDir, "--update-state-dir", UpdaterStateDir())
	if err != nil {
		return fmt.Errorf("create the updater service: %w", err)
	}
	defer s.Close()
	if err := s.SetRecoveryActions([]mgr.RecoveryAction{
		{Type: mgr.ServiceRestart, Delay: 3 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 3 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 30 * time.Second},
	}, 24*3600); err != nil {
		return fmt.Errorf("set recovery actions of the updater: %w", err)
	}
	// The updater ends with a non-zero code after an update but reports "stopped" to the SCM; for
	// the SCM that is a failure without a crash, and recovery actions apply to it only with this
	// flag (live 2026-11-09: the updater stayed stopped after every update on Windows).
	if err := s.SetRecoveryActionsOnNonCrashFailures(true); err != nil {
		return fmt.Errorf("let the recovery actions apply to a non-zero exit of the updater: %w", err)
	}
	if start {
		if err := s.Start(); err != nil {
			return fmt.Errorf("start the updater: %w", err)
		}
	}
	fmt.Fprintf(w, "installed service %s (runs as LocalSystem)\n", UpdaterName)
	return nil
}

// NewHost is the updater's view of this machine: the service manager for the node service and the
// input helper task, plain file moves for the program. UNVERIFIED on real hardware.
func NewHost() update.Host {
	return &fileHost{
		program: filepath.Join(programDir(), Name+".exe"),
		stop: func(context.Context) error {
			m, err := mgr.Connect()
			if err != nil {
				return err
			}
			defer m.Disconnect() //nolint:errcheck // best effort
			if s, err := m.OpenService(Name); err == nil {
				stopService(s)
				_ = s.Close()
			}
			return freeExecutable(Name+".exe", windowsExeOps(), io.Discard)
		},
		start: func(context.Context) error {
			m, err := mgr.Connect()
			if err != nil {
				return err
			}
			defer m.Disconnect() //nolint:errcheck // best effort
			s, err := m.OpenService(Name)
			if err != nil {
				return err
			}
			defer s.Close()
			if err := s.Start(); err != nil && !errors.Is(err, windows.ERROR_SERVICE_ALREADY_RUNNING) {
				return err
			}
			_, _ = schtasks("/Run", "/TN", InputHelperTask) // the helper was ended with the old program
			return nil
		},
	}
}

// schtasks is the task scheduler's command line tool.
func schtasks(args ...string) ([]byte, error) {
	root := os.Getenv("SystemRoot")
	if root == "" {
		root = `C:\Windows`
	}
	return exec.Command(filepath.Join(root, "System32", "schtasks.exe"), args...).CombinedOutput() //nolint:gosec // fixed tool, fixed arguments
}

// netsh is the firewall tool (UNVERIFIED: Go quotes an argument that contains a space as a whole,
// "program=C:\Program Files\...", which netsh is expected to accept).
func netsh(args ...string) ([]byte, error) {
	root := os.Getenv("SystemRoot")
	if root == "" {
		root = `C:\Windows`
	}
	return exec.Command(filepath.Join(root, "System32", "netsh.exe"), args...).CombinedOutput() //nolint:gosec // fixed tool, arguments built from constants and the program path
}

// installInputHelper registers the scheduled task that starts `topcoat-node input-helper` at
// logon in the session of the user (interactive token: only while logged on, no stored password)
// and starts it once for the users who are logged on already.
func installInputHelper(exe string, w io.Writer) error {
	f, err := os.CreateTemp("", "topcoat-input-*.xml")
	if err != nil {
		return err
	}
	_, werr := f.Write(utf16LEWithBOM(InputHelperTaskXML(exe)))
	if cerr := f.Close(); werr == nil {
		werr = cerr
	}
	if werr != nil {
		return werr
	}
	if out, err := schtasks("/Create", "/TN", InputHelperTask, "/XML", f.Name(), "/F"); err != nil {
		// schtasks names only its first complaint about the XML: keep the file so it can be tried
		// by hand (`schtasks /Create /TN topcoat-node-input /XML <file> /F`) and the next one reported
		return fmt.Errorf("schtasks /Create: %w: %s (the task definition is kept in %s)", err, strings.TrimSpace(string(out)), f.Name())
	}
	_ = os.Remove(f.Name()) //nolint:errcheck // best effort
	fmt.Fprintf(w, "scheduled task %s starts the input helper when a user logs on\n", InputHelperTask)
	// best effort: for the sessions that exist now; otherwise it starts at the next logon
	if out, err := schtasks("/Run", "/TN", InputHelperTask); err != nil {
		fmt.Fprintf(w, "the input helper starts at the next logon (starting it now failed: %v: %s)\n", err, out)
	}
	return nil
}

// stopInputHelper ends the running helpers (the task's instances); nothing running is fine.
func stopInputHelper() { _, _ = schtasks("/End", "/TN", InputHelperTask) }

// removeInputHelper ends the helpers and deletes the task; a task that does not exist is fine.
func removeInputHelper(w io.Writer) {
	stopInputHelper()
	if out, err := schtasks("/Delete", "/TN", InputHelperTask, "/F"); err == nil {
		fmt.Fprintf(w, "removed scheduled task %s\n", InputHelperTask)
	} else if _, statErr := schtasks("/Query", "/TN", InputHelperTask); statErr == nil {
		fmt.Fprintf(w, "could not remove scheduled task %s: %v: %s\n", InputHelperTask, err, out)
	}
}

// Restart stops and starts the service. It needs an elevated shell.
func Restart(o RestartOptions) error {
	w := o.Out
	if w == nil {
		w = io.Discard
	}
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect to the service manager (run in an elevated PowerShell): %w", err)
	}
	defer m.Disconnect() //nolint:errcheck // best effort
	s, err := m.OpenService(Name)
	if err != nil {
		return ErrNotInstalled
	}
	defer s.Close()
	stopService(s)
	if err := s.Start(); err != nil {
		return fmt.Errorf("start service: %w", err)
	}
	fmt.Fprintf(w, "restarted service %s\n", Name)
	return nil
}

func stopService(s *mgr.Service) {
	st, err := s.Control(svc.Stop)
	if err != nil {
		return
	}
	for i := 0; i < 60 && st.State != svc.Stopped; i++ {
		time.Sleep(500 * time.Millisecond)
		if st, err = s.Query(); err != nil {
			return
		}
	}
}

// Uninstall stops and deletes the service; Purge also deletes the data dir
// and the program directory.
func Uninstall(o UninstallOptions) error {
	w := o.Out
	if w == nil {
		w = io.Discard
	}
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect to the service manager (run in an elevated PowerShell): %w", err)
	}
	defer m.Disconnect() //nolint:errcheck // best effort
	if s, err := m.OpenService(UpdaterName); err == nil {
		stopService(s)
		_ = s.Delete()
		_ = s.Close()
		fmt.Fprintf(w, "removed service %s\n", UpdaterName)
	}
	if s, err := m.OpenService(Name); err == nil {
		stopService(s)
		if err := s.Delete(); err != nil {
			_ = s.Close()
			return err
		}
		_ = s.Close()
		fmt.Fprintf(w, "removed service %s\n", Name)
	}
	removeInputHelper(w)
	removeTransferFirewallRule(netsh, w)
	if o.Purge {
		// nothing may run from the program directory that is about to be deleted
		if err := freeExecutable(Name+".exe", windowsExeOps(), w); err != nil {
			fmt.Fprintf(w, "%v\n", err)
		}
	}
	// The SID is computed, so this works after the service is gone too.
	if err := groupMembership(false); err != nil {
		fmt.Fprintf(w, "could not remove %s from Performance Monitor Users: %v\n", Account, err)
	}
	if !o.Purge && o.DataDir != "" {
		if _, err := os.Stat(o.DataDir); err == nil {
			if err := secureDir(o.DataDir, ""); err != nil {
				fmt.Fprintf(w, "could not remove %s from the ACL of %s: %v\n", Account, o.DataDir, err)
			}
		}
	}
	if o.Purge {
		if o.DataDir != "" {
			if err := os.RemoveAll(o.DataDir); err != nil {
				return err
			}
			fmt.Fprintf(w, "deleted %s\n", o.DataDir)
		}
		_ = os.RemoveAll(UpdaterStateDir())
		// The running uninstaller may be the copy in programDir; Windows
		// cannot delete a running exe, so errors are only reported.
		if err := os.RemoveAll(programDir()); err != nil {
			fmt.Fprintf(w, "could not delete %s (delete it manually): %v\n", programDir(), err)
		}
	}
	return nil
}

func sameFile(a, b string) bool {
	sa, err1 := os.Stat(a)
	sb, err2 := os.Stat(b)
	return err1 == nil && err2 == nil && os.SameFile(sa, sb)
}

// IsWindowsService reports whether the process was started by the SCM.
// EnsureUpdaterRestarts sets, on the updater's own service, that the SCM restarts it after a
// non-zero exit (see installUpdater). The updater runs as LocalSystem and may change its own
// service; it does this at every start, so an installation made before 0.2.8 is repaired by the
// first start of the new updater, without a new "topcoat-node install".
func EnsureUpdaterRestarts() error {
	m, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer m.Disconnect() //nolint:errcheck // nothing to do about it
	s, err := m.OpenService(UpdaterName)
	if err != nil {
		return err
	}
	defer s.Close()
	return s.SetRecoveryActionsOnNonCrashFailures(true)
}

func IsWindowsService() bool {
	ok, err := svc.IsWindowsService()
	return err == nil && ok
}

type handler struct {
	run  func(stop <-chan struct{}) int
	code int
}

func (h *handler) Execute(_ []string, r <-chan svc.ChangeRequest, s chan<- svc.Status) (bool, uint32) {
	s <- svc.Status{State: svc.StartPending}
	stop := make(chan struct{})
	done := make(chan int, 1)
	go func() { done <- h.run(stop) }()
	s <- svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown}
	for {
		select {
		case code := <-done:
			h.code = code
			// Non-zero exit makes the SCM apply the recovery actions (restart).
			return code != 0, uint32(code)
		case c := <-r:
			switch c.Cmd {
			case svc.Interrogate:
				s <- c.CurrentStatus
			case svc.Stop, svc.Shutdown:
				s <- svc.Status{State: svc.StopPending}
				close(stop)
				select {
				case h.code = <-done:
				case <-time.After(25 * time.Second):
				}
				return false, 0
			}
		}
	}
}

// RunWindowsService runs fn under the service control manager; fn must
// return when stop is closed.
func RunWindowsService(name string, fn func(stop <-chan struct{}) int) int {
	h := &handler{run: fn}
	if err := svc.Run(name, h); err != nil {
		return 1
	}
	return h.code
}

// windowsExeOps is freeExecutable on this machine: the scheduled task ended with schtasks, the
// processes listed with a Toolhelp snapshot and ended with TerminateProcess (install runs
// elevated).
func windowsExeOps() exeOps {
	return exeOps{
		endTask: stopInputHelper,
		list: func() ([]exeProcess, error) {
			snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
			if err != nil {
				return nil, err
			}
			defer windows.CloseHandle(snap) //nolint:errcheck // best effort
			var pe windows.ProcessEntry32
			pe.Size = uint32(unsafe.Sizeof(pe))
			var out []exeProcess
			for err = windows.Process32First(snap, &pe); err == nil; err = windows.Process32Next(snap, &pe) {
				out = append(out, exeProcess{PID: pe.ProcessID, Name: windows.UTF16ToString(pe.ExeFile[:])})
			}
			return out, nil
		},
		kill: func(pid uint32) error {
			h, err := windows.OpenProcess(windows.PROCESS_TERMINATE, false, pid)
			if err != nil {
				if errors.Is(err, windows.ERROR_INVALID_PARAMETER) {
					return nil // gone already
				}
				return err
			}
			defer windows.CloseHandle(h) //nolint:errcheck // best effort
			return windows.TerminateProcess(h, 1)
		},
		waitGone: func(pid uint32, d time.Duration) bool {
			h, err := windows.OpenProcess(windows.SYNCHRONIZE, false, pid)
			if err != nil {
				return true // cannot be opened: gone (or not ours to wait for)
			}
			defer windows.CloseHandle(h)                                          //nolint:errcheck // best effort
			ev, err := windows.WaitForSingleObject(h, uint32(d/time.Millisecond)) //nolint:gosec // d is a few seconds
			return err == nil && ev == windows.WAIT_OBJECT_0
		},
		selfPID:   windows.GetCurrentProcessId(),
		waitLimit: 10 * time.Second,
	}
}
