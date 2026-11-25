package inputhelper

import (
	"errors"
	"fmt"
	"io"
	"os"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"

	"topcoat/node/internal/winproc"
)

// UNVERIFIED on real hardware: everything in this file — whether the scheduled task starts the
// helper in the console session, that the service's SID may open the pipe with this ACL, and that
// CancelIoEx ends a blocked read of the client.

var (
	user32               = windows.NewLazySystemDLL("user32.dll")
	kernel32             = windows.NewLazySystemDLL("kernel32.dll")
	procGetLastInputInfo = user32.NewProc("GetLastInputInfo")
	procShowWindow       = user32.NewProc("ShowWindow")
	procGetConsoleWindow = kernel32.NewProc("GetConsoleWindow")
	procFreeConsole      = kernel32.NewProc("FreeConsole")
	procGetTickCount     = kernel32.NewProc("GetTickCount")
)

const swHide = 0

// handleRW reads and writes a pipe handle synchronously.
type handleRW struct{ h windows.Handle }

func (p handleRW) Read(b []byte) (int, error) {
	var n uint32
	if err := windows.ReadFile(p.h, b, &n, nil); err != nil {
		if errors.Is(err, windows.ERROR_BROKEN_PIPE) || errors.Is(err, windows.ERROR_PIPE_NOT_CONNECTED) {
			return int(n), io.EOF
		}
		return int(n), err
	}
	return int(n), nil
}

func (p handleRW) Write(b []byte) (int, error) {
	var n uint32
	err := windows.WriteFile(p.h, b, &n, nil)
	return int(n), err
}

type lastInputInfo struct {
	Size uint32
	Time uint32
}

// idleNow is the time since the last keyboard or mouse input of this session (GetLastInputInfo,
// which measures the calling process's session only).
func idleNow() (time.Duration, error) {
	info := lastInputInfo{Size: uint32(unsafe.Sizeof(lastInputInfo{}))}
	if r, _, err := procGetLastInputInfo.Call(uintptr(unsafe.Pointer(&info))); r == 0 {
		return 0, fmt.Errorf("GetLastInputInfo: %w", err)
	}
	tick, _, _ := procGetTickCount.Call()
	return time.Duration(uint32(tick)-info.Time) * time.Millisecond, nil // both wrap at 49 days alike
}

// hideConsole makes the helper invisible: a console program started by the task scheduler would
// otherwise keep a window open in the user's session.
func hideConsole() {
	if w, _, _ := procGetConsoleWindow.Call(); w != 0 {
		_, _, _ = procShowWindow.Call(w, swHide)
	}
	_, _, _ = procFreeConsole.Call()
}

// Run is the helper: it serves the idle time of its own session on the named pipe until it is
// killed (logoff, the scheduled task ended by install/uninstall). serviceSID is the only account
// that may connect. It returns ErrAlreadyRunning if another helper serves this session.
func Run(serviceSID string, logf func(format string, args ...any)) error {
	hideConsole()
	var session uint32
	if err := windows.ProcessIdToSessionId(windows.GetCurrentProcessId(), &session); err != nil {
		return fmt.Errorf("session of the helper: %w", err)
	}
	sd, err := windows.SecurityDescriptorFromString(PipeSDDL(serviceSID))
	if err != nil {
		return fmt.Errorf("pipe ACL: %w", err)
	}
	sa := &windows.SecurityAttributes{Length: uint32(unsafe.Sizeof(windows.SecurityAttributes{})), SecurityDescriptor: sd}
	name, err := windows.UTF16PtrFromString(PipeName(session))
	if err != nil {
		return err
	}
	// One instance only (FIRST_PIPE_INSTANCE, max 1): a second instance would need the
	// create-instance right, which the ACL does not give anybody, and a second helper is refused.
	h, err := windows.CreateNamedPipe(name,
		windows.PIPE_ACCESS_DUPLEX|windows.FILE_FLAG_FIRST_PIPE_INSTANCE,
		windows.PIPE_TYPE_BYTE|windows.PIPE_READMODE_BYTE|windows.PIPE_WAIT|windows.PIPE_REJECT_REMOTE_CLIENTS,
		1, 512, 512, 0, sa)
	if err != nil {
		if errors.Is(err, windows.ERROR_ACCESS_DENIED) || errors.Is(err, windows.ERROR_PIPE_BUSY) {
			return ErrAlreadyRunning
		}
		return fmt.Errorf("create the pipe %s: %w", PipeName(session), err)
	}
	defer windows.CloseHandle(h) //nolint:errcheck // process exit
	logf("serving session %d on %s for %s", session, PipeName(session), serviceSID)
	for {
		if err := windows.ConnectNamedPipe(h, nil); err != nil && !errors.Is(err, windows.ERROR_PIPE_CONNECTED) {
			logf("accept: %v", err)
			time.Sleep(time.Second)
			continue
		}
		if err := Serve(handleRW{h}, idleNow); err != nil {
			logf("client: %v", err)
		}
		_ = windows.DisconnectNamedPipe(h)
	}
}

// pipeProbe asks Windows who serves the pipe behind h and what that process is. It never asks for
// the session of the pipe itself (GetNamedPipeServerSessionId): the process id is resolved, and
// session and program file are read from the process list.
func pipeProbe(h windows.Handle) serverProbe {
	return serverProbe{
		pipeServerPID: func() (uint32, error) {
			var pid uint32
			err := windows.GetNamedPipeServerProcessId(h, &pid)
			return pid, err
		},
		process: func(pid uint32) (Server, error) {
			// NtQuerySystemInformation, not OpenProcess / ProcessIdToSessionId /
			// QueryFullProcessImageName: the service account may not open the user's process
			// ("Zugriff verweigert" in the hardware test), but it may read the process list.
			srv := Server{PID: pid}
			var err error
			if srv.Session, err = winproc.SessionOf(pid); err != nil {
				return srv, fmt.Errorf("session of the process: %w", err)
			}
			if srv.Image, err = winproc.ImagePath(pid); err != nil {
				return srv, fmt.Errorf("program file of the process (session %d): %w", srv.Session, err)
			}
			return srv, nil
		},
	}
}

// installedImage is the program file of this (the service's) process; the helper is the same file.
func installedImage() string {
	p, err := os.Executable()
	if err != nil {
		return ""
	}
	return p
}

// queryTimeout bounds one question to the helper.
const queryTimeout = 2 * time.Second

// IdleFromHelper asks the helper of a session (the one at the console) for the time since the last
// input. ErrNoHelper means nothing serves that session. The pipe must be served by a process of
// that session that runs the installed program file (verifyServer) — nobody else can pose as the
// console user's helper.
func IdleFromHelper(session uint32) (time.Duration, error) {
	name, err := windows.UTF16PtrFromString(PipeName(session))
	if err != nil {
		return 0, err
	}
	var h windows.Handle
	for range 10 {
		// anonymous impersonation level: the pipe's server cannot act as this service
		h, err = windows.CreateFile(name, windows.GENERIC_READ|windows.GENERIC_WRITE, 0, nil,
			windows.OPEN_EXISTING, windows.SECURITY_SQOS_PRESENT|windows.SECURITY_ANONYMOUS, 0)
		if err == nil {
			break
		}
		switch {
		case errors.Is(err, windows.ERROR_FILE_NOT_FOUND), errors.Is(err, windows.ERROR_PATH_NOT_FOUND):
			return 0, ErrNoHelper
		case errors.Is(err, windows.ERROR_PIPE_BUSY):
			time.Sleep(100 * time.Millisecond)
			continue
		case errors.Is(err, windows.ERROR_ACCESS_DENIED):
			return 0, fmt.Errorf("the input helper's pipe refuses this account: %w", err)
		}
		return 0, err
	}
	if err != nil {
		return 0, fmt.Errorf("the input helper is busy: %w", err)
	}
	defer windows.CloseHandle(h) //nolint:errcheck // best effort
	if err := verifyServer(pipeProbe(h), session, installedImage()); err != nil {
		return 0, err
	}
	type result struct {
		d   time.Duration
		err error
	}
	done := make(chan result, 1)
	go func() {
		d, err := Query(handleRW{h})
		done <- result{d, err}
	}()
	select {
	case r := <-done:
		return r.d, r.err
	case <-time.After(queryTimeout):
		_ = windows.CancelIoEx(h, nil) // ends the blocked read
		<-done
		return 0, errors.New("the input helper did not answer in time")
	}
}
