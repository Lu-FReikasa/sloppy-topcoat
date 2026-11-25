package inputhelper

import (
	"errors"
	"io"
	"net"
	"strings"
	"testing"
	"time"
)

func serve(t *testing.T, idle func() (time.Duration, error)) net.Conn {
	t.Helper()
	c, s := net.Pipe()
	go func() {
		_ = Serve(s, idle)
		_ = s.Close()
	}()
	t.Cleanup(func() { _ = c.Close() })
	return c
}

func TestTheHelperAnswersTheIdleTime(t *testing.T) {
	c := serve(t, func() (time.Duration, error) { return 12345 * time.Millisecond, nil })
	d, err := Query(c)
	if err != nil || d != 12345*time.Millisecond {
		t.Fatalf("%v %v", d, err)
	}
	d, err = Query(c) // the connection serves more than one request
	if err != nil || d != 12345*time.Millisecond {
		t.Fatalf("second: %v %v", d, err)
	}
}

func TestAnErrorOfTheHelperReachesTheService(t *testing.T) {
	c := serve(t, func() (time.Duration, error) { return 0, errors.New("GetLastInputInfo failed") })
	if _, err := Query(c); err == nil || !strings.Contains(err.Error(), "GetLastInputInfo failed") {
		t.Fatalf("%v", err)
	}
}

func TestUnknownRequestsAndWrongVersionsAreRefused(t *testing.T) {
	c, s := net.Pipe()
	defer c.Close() //nolint:errcheck // test
	go func() { _ = Serve(s, func() (time.Duration, error) { return 0, nil }); _ = s.Close() }()
	_, _ = io.WriteString(c, "shell\n")
	buf := make([]byte, 128)
	n, _ := c.Read(buf)
	if !strings.Contains(string(buf[:n]), "unknown request") {
		t.Fatalf("%q", buf[:n])
	}

	// a helper of another wire version
	c2, s2 := net.Pipe()
	defer c2.Close() //nolint:errcheck // test
	go func() {
		buf := make([]byte, 16)
		_, _ = s2.Read(buf)
		_, _ = io.WriteString(s2, "{\"v\":7,\"idle_ms\":1}\n")
	}()
	if _, err := Query(c2); err == nil || !strings.Contains(err.Error(), "version 7") {
		t.Fatalf("%v", err)
	}
}

func TestALineThatNeverEndsIsNotBuffered(t *testing.T) {
	c, s := net.Pipe()
	defer c.Close() //nolint:errcheck // test
	done := make(chan error, 1)
	go func() { done <- Serve(s, func() (time.Duration, error) { return 0, nil }) }()
	go func() { _, _ = io.WriteString(c, strings.Repeat("x", 4*maxLine)) }()
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "too long") {
			t.Fatalf("%v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("the server buffers an endless line")
	}
	_ = s.Close()
}

func TestAClosedHelperIsAnUnexpectedEnd(t *testing.T) {
	c, s := net.Pipe()
	go func() {
		buf := make([]byte, 16)
		_, _ = s.Read(buf)
		_ = s.Close()
	}()
	if _, err := Query(c); !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("%v", err)
	}
}

func TestThePipeIsNamedPerSessionAndOnlyTheServiceMayConnect(t *testing.T) {
	if got := PipeName(3); got != `\\.\pipe\topcoat-node-input-3` {
		t.Fatal(got)
	}
	sddl := PipeSDDL("S-1-5-80-1-2-3-4-5")
	if sddl != "D:P(A;;GRGW;;;S-1-5-80-1-2-3-4-5)" || strings.Count(sddl, "(A;") != 1 {
		t.Fatal(sddl)
	}
}

func probe(pid uint32, pidErr error, srv Server, procErr error) serverProbe {
	return serverProbe{
		pipeServerPID: func() (uint32, error) { return pid, pidErr },
		process:       func(uint32) (Server, error) { return srv, procErr },
	}
}

const installedExe = `C:\Program Files\topcoat-node\topcoat-node.exe`

func TestAHelperInTheConsoleSessionRunningTheInstalledProgramIsTrusted(t *testing.T) {
	ok := Server{PID: 4242, Session: 1, Image: installedExe}
	if err := verifyServer(probe(4242, nil, ok, nil), 1, installedExe); err != nil {
		t.Fatal(err)
	}
	// Windows paths differ in case and prefix, not in meaning
	other := Server{PID: 4242, Session: 1, Image: `\\?\c:\program files\TOPCOAT-NODE\topcoat-node.exe`}
	if err := verifyServer(probe(4242, nil, other, nil), 1, installedExe); err != nil {
		t.Fatal(err)
	}
}

func TestAPipeServedFromAnotherSessionOrProgramOrUnknownIsNotTrusted(t *testing.T) {
	for name, c := range map[string]struct {
		p    serverProbe
		want string
	}{
		"session 0":       {probe(10, nil, Server{PID: 10, Session: 0, Image: installedExe}, nil), "in session 0"},
		"another session": {probe(10, nil, Server{PID: 10, Session: 2, Image: installedExe}, nil), "in session 2"},
		"another program": {probe(10, nil, Server{PID: 10, Session: 1, Image: `C:\Users\x\AppData\evil.exe`}, nil), "not the installed"},
		"no image":        {probe(10, nil, Server{PID: 10, Session: 1}, nil), "not the installed"},
		"no pid":          {probe(0, nil, Server{}, nil), "pid 0"},
		"pipe api fails":  {probe(0, errors.New("boom"), Server{}, nil), "who serves the pipe: boom"},
		"process denied":  {probe(10, nil, Server{}, errors.New("access is denied")), "(pid 10) cannot be examined: access is denied"},
	} {
		t.Run(name, func(t *testing.T) {
			err := verifyServer(c.p, 1, installedExe)
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("want an error with %q, got %v", c.want, err)
			}
		})
	}
}
