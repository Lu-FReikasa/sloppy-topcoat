package servicebus

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"os"
	"runtime"
	"strings"
	"testing"
	"time"
)

func tmpDir(t *testing.T) string {
	t.Helper()
	// unix socket paths are short: use a short temp directory
	d, err := os.MkdirTemp("", "sb")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(d) })
	return d
}

// newBus starts a bus whose peers all have the given uid, mapped to the given services.
func newBus(t *testing.T, uid uint32, byUID map[uint32]string, specs ...Spec) (*Bus, context.Context) {
	t.Helper()
	b, err := New(tmpDir(t), specs, func(u uint32) (string, bool) { s, ok := byUID[u]; return s, ok })
	if err != nil {
		t.Fatal(err)
	}
	b.peer = func(net.Conn) (uint32, error) { return uid, nil }
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	if err := b.Listen(ctx, nil); err != nil {
		t.Fatal(err)
	}
	return b, ctx
}

func serve(t *testing.T, ctx context.Context, b *Bus, service string, actions []string, h Handler) *Client { //nolint:revive // test helper: t comes first by convention
	t.Helper()
	cl, err := Dial(ctx, b.SocketPath(service), service, actions)
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = cl.Serve(ctx, h) }()
	waitFor(t, func() bool { return b.Connected(service) })
	return cl
}

func waitFor(t *testing.T, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for !ok() {
		if time.Now().After(deadline) {
			t.Fatal("timed out")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func echo(_ context.Context, action string, in json.RawMessage) (json.RawMessage, error) {
	if action == "build.fail" {
		return nil, errors.New("it broke")
	}
	return json.RawMessage(`{"action":"` + action + `","input":` + string(in) + `}`), nil
}

func TestACallReachesTheServiceAndComesBack(t *testing.T) {
	b, ctx := newBus(t, 1000, map[uint32]string{1000: "build"}, Spec{ID: "build", Actions: []string{"build.fetch", "build.fail"}})
	serve(t, ctx, b, "build", []string{"build.fetch", "build.fail"}, echo)
	out, err := b.Call(ctx, "build", "build.fetch", json.RawMessage(`{"ref":"main"}`))
	if err != nil || string(out) != `{"action":"build.fetch","input":{"ref":"main"}}` {
		t.Fatalf("out=%s err=%v", out, err)
	}
	if _, err := b.Call(ctx, "build", "build.fail", json.RawMessage(`{}`)); err == nil || err.Error() != "it broke" {
		t.Fatalf("the service's error must come back as it is, got %v", err)
	}
}

func TestOnlyDeclaredAndAllowedActionsAreCalled(t *testing.T) {
	b, ctx := newBus(t, 1000, map[uint32]string{1000: "build"}, Spec{ID: "build", Actions: []string{"build.fetch"}})
	serve(t, ctx, b, "build", []string{"build.fetch"}, echo)
	for _, tc := range []struct{ service, action string }{
		{"build", "build.hub_image"}, {"build", "shell.run"}, {"other", "build.fetch"},
	} {
		if _, err := b.Call(ctx, tc.service, tc.action, nil); !errors.Is(err, ErrNotAllowed) {
			t.Errorf("%v: want ErrNotAllowed, got %v", tc, err)
		}
	}
}

func TestAServiceCannotDeclareMoreThanTheConfigAllows(t *testing.T) {
	b, ctx := newBus(t, 1000, map[uint32]string{1000: "build"}, Spec{ID: "build", Actions: []string{"build.fetch"}})
	cl, err := Dial(ctx, b.SocketPath("build"), "build", []string{"build.fetch", "build.evil"})
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = cl.Serve(ctx, echo) }()
	time.Sleep(150 * time.Millisecond)
	if b.Connected("build") {
		t.Fatal("a hello with an action outside the whitelist must be refused")
	}
	if _, err := b.Call(ctx, "build", "build.fetch", nil); !errors.Is(err, ErrNotConnected) {
		t.Fatalf("got %v", err)
	}
}

func TestAPeerThatTheKernelSaysIsSomebodyElseIsNeverHeard(t *testing.T) {
	// the build socket is reached by uid 2000, which belongs to the "tools" service
	b, ctx := newBus(t, 2000, map[uint32]string{1000: "build", 2000: "tools"},
		Spec{ID: "build", Actions: []string{"build.fetch"}}, Spec{ID: "tools", Actions: []string{"tools.ps"}})
	// the bus may close before the hello is written: a refused dial is as good as a refused hello
	if cl, err := Dial(ctx, b.SocketPath("build"), "build", []string{"build.fetch"}); err == nil {
		go func() { _ = cl.Serve(ctx, echo) }()
	}
	time.Sleep(150 * time.Millisecond)
	if b.Connected("build") {
		t.Fatal("uid 2000 is not the build service")
	}
	// and an unknown uid is nobody
	b2, ctx2 := newBus(t, 4242, map[uint32]string{1000: "build"}, Spec{ID: "build", Actions: []string{"build.fetch"}})
	if cl2, err := Dial(ctx2, b2.SocketPath("build"), "build", []string{"build.fetch"}); err == nil {
		go func() { _ = cl2.Serve(ctx2, echo) }()
	}
	time.Sleep(150 * time.Millisecond)
	if b2.Connected("build") {
		t.Fatal("an unknown uid must not connect")
	}
}

func TestAServiceCannotPoseAsAnotherInItsHello(t *testing.T) {
	b, ctx := newBus(t, 1000, map[uint32]string{1000: "build"}, Spec{ID: "build", Actions: []string{"build.fetch"}})
	cl, err := Dial(ctx, b.SocketPath("build"), "tools", []string{"build.fetch"})
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = cl.Serve(ctx, echo) }()
	time.Sleep(150 * time.Millisecond)
	if b.Connected("build") || b.Connected("tools") {
		t.Fatal("the hello names another service than the socket's user")
	}
}

func TestAServiceMayAskTheNodeForNamedThingsOnly(t *testing.T) {
	b, ctx := newBus(t, 1000, map[uint32]string{1000: "build"}, Spec{ID: "build", Actions: []string{"build.fetch"}})
	if err := b.Handle("upload", func(_ context.Context, service string, in json.RawMessage) (json.RawMessage, error) {
		return json.RawMessage(`{"by":"` + service + `","got":` + string(in) + `}`), nil
	}); err != nil {
		t.Fatal(err)
	}
	if err := b.Handle("Bad Name", nil); err == nil {
		t.Fatal("a host function name is checked")
	}
	cl := serve(t, ctx, b, "build", []string{"build.fetch"}, echo)
	out, err := cl.Host(ctx, "upload", map[string]string{"name": "x"})
	if err != nil || string(out) != `{"by":"build","got":{"name":"x"}}` {
		t.Fatalf("out=%s err=%v", out, err)
	}
	if _, err := cl.Host(ctx, "shell", map[string]string{"cmd": "ls"}); err == nil || !strings.Contains(err.Error(), "unknown host function") {
		t.Fatalf("an unknown host function must be refused, got %v", err)
	}
}

func TestACallEndsWhenTheServiceGoes(t *testing.T) {
	b, ctx := newBus(t, 1000, map[uint32]string{1000: "build"}, Spec{ID: "build", Actions: []string{"build.fetch"}})
	block := make(chan struct{})
	cl := serve(t, ctx, b, "build", []string{"build.fetch"}, func(context.Context, string, json.RawMessage) (json.RawMessage, error) {
		<-block
		return nil, nil
	})
	done := make(chan error, 1)
	go func() { _, err := b.Call(ctx, "build", "build.fetch", nil); done <- err }()
	time.Sleep(100 * time.Millisecond)
	_ = cl.c.Close()
	select {
	case err := <-done:
		if !errors.Is(err, ErrClosed) {
			t.Fatalf("got %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("the call hangs after the service went away")
	}
	close(block)
	waitFor(t, func() bool { return !b.Connected("build") })
}

func TestACallHonoursItsContext(t *testing.T) {
	b, ctx := newBus(t, 1000, map[uint32]string{1000: "build"}, Spec{ID: "build", Actions: []string{"build.fetch"}})
	block := make(chan struct{})
	defer close(block)
	serve(t, ctx, b, "build", []string{"build.fetch"}, func(context.Context, string, json.RawMessage) (json.RawMessage, error) {
		<-block
		return nil, nil
	})
	short, cancel := context.WithTimeout(ctx, 100*time.Millisecond)
	defer cancel()
	if _, err := b.Call(short, "build", "build.fetch", nil); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("got %v", err)
	}
}

func TestTheSocketIsForItsOwnerOnlyAndTheNamesAreChecked(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix socket modes do not exist on Windows")
	}
	b, _ := newBus(t, 1000, map[uint32]string{1000: "build"}, Spec{ID: "build", Actions: []string{"build.fetch"}})
	fi, err := os.Stat(b.SocketPath("build"))
	if err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("socket mode: %v %v", fi, err)
	}
	if _, err := New(t.TempDir(), []Spec{{ID: "../x"}}, nil); err == nil {
		t.Fatal("a service name is checked")
	}
	if _, err := New(t.TempDir(), []Spec{{ID: "build", Actions: []string{"rm -rf"}}}, nil); err == nil {
		t.Fatal("an action name is checked")
	}
}

func TestTheRealKernelNamesTheCaller(t *testing.T) {
	dir := tmpDir(t)
	ln, err := net.Listen("unix", dir+"/p.sock")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = ln.Close() }()
	got := make(chan uint32, 1)
	go func() {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		defer func() { _ = c.Close() }()
		uid, err := PeerUID(c)
		if err != nil {
			got <- ^uint32(0)
			return
		}
		got <- uid
	}()
	c, err := net.Dial("unix", dir+"/p.sock")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close() }()
	uid := <-got
	if uid == ^uint32(0) {
		t.Skip("peer credentials are not available here")
	}
	if int(uid) != os.Getuid() {
		t.Fatalf("PeerUID = %d, this process runs as %d", uid, os.Getuid())
	}
}
