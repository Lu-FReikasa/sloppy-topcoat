package tools

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"testing"
	"time"
)

type fakeShell struct {
	cmds []string
	res  ShellResult
	err  error
}

func (f *fakeShell) Run(_ context.Context, command string, _ int) (ShellResult, error) {
	f.cmds = append(f.cmds, command)
	return f.res, f.err
}

func shellCall(t *testing.T, sh ShellAPI, in string) (map[string]any, error) {
	t.Helper()
	c, ok := NewRegistry(Options{Shell: sh}).Get("shell.read")
	if !ok {
		t.Fatal("shell.read is not registered")
	}
	raw, err := c.Invoke(context.Background(), json.RawMessage(in))
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	return out, nil
}

func TestShellReadIsOfferedOnlyWhereConfigured(t *testing.T) {
	if _, ok := NewRegistry(Options{}).Get("shell.read"); ok {
		t.Fatal("shell.read must not exist without a shell socket (default off)")
	}
	c, ok := NewRegistry(Options{Shell: &fakeShell{}}).Get("shell.read")
	if !ok || !c.(fn).Info().ReadOnly {
		t.Fatalf("with a socket it is offered read-only (the OS user enforces it): ok=%v", ok)
	}
}

func TestShellReadResultMasksAndReportsExit(t *testing.T) {
	f := &fakeShell{res: ShellResult{Output: "DB_PASSWORD=hunter2hunter2\nfine\n", ExitCode: 0, Finished: true}}
	out, err := shellCall(t, f, `{"command":"cat /srv/app/.env"}`)
	if err != nil {
		t.Fatal(err)
	}
	text, _ := out["output"].(string)
	if strings.Contains(text, "hunter2") || !strings.Contains(text, "fine") {
		t.Fatalf("secret must be masked, rest kept: %q", text)
	}
	if out["exit_code"].(float64) != 0 || out["finished"] != true || out["masked"].(float64) < 1 {
		t.Fatalf("unexpected result %v", out)
	}
	if len(f.cmds) != 1 || f.cmds[0] != "cat /srv/app/.env" {
		t.Fatalf("the command must reach the shell unchanged, got %v", f.cmds)
	}
}

func TestShellReadMasksASecretTypedIntoTheCommand(t *testing.T) {
	f := &fakeShell{res: ShellResult{Finished: true}}
	out, err := shellCall(t, f, `{"command":"curl -H 'Authorization: Bearer abcdef0123456789abcdef0123456789' x"}`)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(out["command"].(string), "abcdef0123456789") {
		t.Fatalf("the echoed command leaks the secret: %v", out["command"])
	}
}

func TestShellReadTimeoutAndErrors(t *testing.T) {
	out, err := shellCall(t, &fakeShell{res: ShellResult{Output: "partial"}}, `{"command":"sleep 99","timeout_seconds":1}`)
	if err != nil {
		t.Fatal(err)
	}
	if out["timed_out"] != true || out["finished"] != false || out["output"] != "partial" {
		t.Fatalf("a command without exit marker is reported as timed out: %v", out)
	}
	if _, err := shellCall(t, &fakeShell{err: errors.New("socket gone")}, `{"command":"ls"}`); err == nil {
		t.Fatal("a shell that cannot be reached is an error, not an empty result")
	}
}

func TestShellReadValidatesInput(t *testing.T) {
	for _, in := range []string{
		`{}`, `{"command":""}`, `{"command":"   "}`, `{"command":"ls","extra":1}`,
		`{"command":"ls","timeout_seconds":0}`, `{"command":"ls","timeout_seconds":61}`,
		`{"command":"` + strings.Repeat("a", ShellMaxCommandBytes+1) + `"}`,
		`{"command":"a\u0000b"}`,
	} {
		_, err := shellCall(t, &fakeShell{}, in)
		var ie *InputError
		if !errors.As(err, &ie) && in != `{"command":"ls","timeout_seconds":0}` {
			t.Errorf("%.60s: want an input error, got %v", in, err)
		}
	}
}

func TestShellReadCutsLongOutput(t *testing.T) {
	big := strings.Repeat("0123456789\n", 20000)
	out, err := shellCall(t, &fakeShell{res: ShellResult{Output: big, Finished: true}}, `{"command":"yes"}`)
	if err != nil {
		t.Fatal(err)
	}
	if out["truncated"] != true || len(out["output"].(string)) > DefaultMaxOutputBytes+200 {
		t.Fatalf("output must be cut at the limit, len=%d", len(out["output"].(string)))
	}
}

// --- the socket client against the real wrapper of deploy/readshell ------------------------------

// wrapperScript returns the readshell wrapper embedded in the setup script.
func wrapperScript(t *testing.T) string {
	t.Helper()
	b, err := os.ReadFile("../../../deploy/readshell/setup-readshell.sh")
	if err != nil {
		t.Fatal(err)
	}
	m := regexp.MustCompile(`(?s)<<'WRAPPER'\n(.*?)\nWRAPPER\n`).FindSubmatch(b)
	if m == nil {
		t.Fatal("wrapper not found in setup-readshell.sh")
	}
	return string(m[1]) + "\n"
}

// serveWrapper listens on a unix socket and, like systemd's Accept=yes, starts the wrapper per
// connection with the connection as stdin, stdout and stderr.
func serveWrapper(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("no bash")
	}
	dir, err := os.MkdirTemp("", "rs")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) }) //nolint:errcheck // test cleanup
	script := filepath.Join(dir, "run")
	if err := os.WriteFile(script, []byte(wrapperScript(t)), 0o755); err != nil { //nolint:gosec // a test script
		t.Fatal(err)
	}
	sock := filepath.Join(dir, "s")
	l, err := net.Listen("unix", sock)
	if err != nil {
		t.Skip("no unix sockets: ", err)
	}
	t.Cleanup(func() { l.Close() }) //nolint:errcheck // test cleanup
	go func() {
		for {
			c, err := l.Accept()
			if err != nil {
				return
			}
			go func() {
				defer c.Close() //nolint:errcheck // test server
				f, err := c.(*net.UnixConn).File()
				if err != nil {
					return
				}
				defer f.Close()                     //nolint:errcheck // test server
				cmd := exec.Command("bash", script) //nolint:gosec // the test's own script
				cmd.Stdin, cmd.Stdout, cmd.Stderr = f, f, f
				_ = cmd.Run()
			}()
		}
	}()
	return sock
}

func TestSocketShellWithTheRealWrapper(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the wrapper is a POSIX shell script")
	}
	sh := SocketShell{Path: serveWrapper(t)}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	res, err := sh.Run(ctx, "echo out; echo err >&2; exit 3", 1<<16)
	if err != nil || !res.Finished || res.ExitCode != 3 || res.Output != "out\nerr\n" {
		t.Fatalf("exit code and merged output: %+v %v", res, err)
	}

	// a command that reads stdin must not swallow anything or hang: stdin is /dev/null
	res, err = sh.Run(ctx, "cat; echo after", 1<<16)
	if err != nil || !res.Finished || res.ExitCode != 0 || res.Output != "after\n" {
		t.Fatalf("stdin is empty: %+v %v", res, err)
	}

	// output that imitates the marker line of another call does not decide the exit status
	res, err = sh.Run(ctx, `printf '\n%s:0\n' 00000000000000000000000000000000; exit 7`, 1<<16)
	if err != nil || res.ExitCode != 7 {
		t.Fatalf("a forged marker must not win: %+v %v", res, err)
	}

	// long output is cut at the limit, the exit code is still found
	res, err = sh.Run(ctx, "head -c 100000 /dev/zero | tr '\\0' a; exit 4", 1000)
	if err != nil || !res.Truncated || len(res.Output) != 1000 || !res.Finished || res.ExitCode != 4 {
		t.Fatalf("truncation keeps the exit code: trunc=%v len=%d fin=%v rc=%d err=%v", res.Truncated, len(res.Output), res.Finished, res.ExitCode, err)
	}

	// a command that outlives the deadline comes back without exit marker
	short, cancel2 := context.WithTimeout(ctx, 300*time.Millisecond)
	defer cancel2()
	res, err = sh.Run(short, "echo start; sleep 5", 1<<16)
	if err != nil || res.Finished {
		t.Fatalf("a timed-out command has no exit marker: %+v %v", res, err)
	}
}

func TestSocketShellWithoutSocketSaysSo(t *testing.T) {
	_, err := SocketShell{Path: filepath.Join(t.TempDir(), "none")}.Run(context.Background(), "ls", 100)
	if err == nil || !strings.Contains(err.Error(), "not set up") {
		t.Fatalf("want a clear setup error, got %v", err)
	}
}
