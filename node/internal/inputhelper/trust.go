package inputhelper

import (
	"fmt"
	"path/filepath"
	"strings"
)

// Server is what the service learns about the process at the other end of the helper's pipe.
type Server struct {
	PID     uint32
	Session uint32
	Image   string // full path of the program file
}

// serverProbe is the operating system side of the check: who serves the pipe, and what is that
// process. Windows implements it (pipe_windows.go); tests use fakes.
type serverProbe struct {
	pipeServerPID func() (uint32, error)
	process       func(pid uint32) (Server, error)
}

// verifyServer decides whether the pipe of a session may be trusted: it must be served by a
// process of the session at the console that runs the installed program file. The pipe's ACL
// keeps everybody but the service out as a *client*; this keeps everybody but the helper in as the
// *server* — any user could create a pipe of that name before the helper does. Every reason to
// refuse names the pid, session and path it saw, so a failure on real hardware can be diagnosed
// from the message alone.
func verifyServer(p serverProbe, wantSession uint32, installed string) error {
	pid, err := p.pipeServerPID()
	if err != nil {
		return fmt.Errorf("who serves the pipe: %w", err)
	}
	if pid == 0 {
		return fmt.Errorf("the pipe reports no server process (pid 0): not trusted")
	}
	srv, err := p.process(pid)
	if err != nil {
		return fmt.Errorf("the pipe's server process (pid %d) cannot be examined: %w", pid, err)
	}
	if srv.Session != wantSession {
		return fmt.Errorf("the pipe of session %d is served by pid %d in session %d (%s): not trusted",
			wantSession, pid, srv.Session, srv.Image)
	}
	if !sameFile(srv.Image, installed) {
		return fmt.Errorf("the pipe of session %d is served by pid %d running %q, not the installed %q: not trusted",
			wantSession, pid, srv.Image, installed)
	}
	return nil
}

// sameFile compares two Windows program paths: case-insensitive, separators and the \\?\ prefix
// ignored, symlinks resolved where that works.
func sameFile(a, b string) bool {
	if a == "" || b == "" {
		return false
	}
	return normalizePath(a) == normalizePath(b)
}

func normalizePath(p string) string {
	p = strings.TrimPrefix(p, `\\?\`)
	if r, err := filepath.EvalSymlinks(p); err == nil {
		p = r
	}
	p = strings.ReplaceAll(p, "/", `\`)
	return strings.ToLower(strings.TrimRight(p, `\`))
}
