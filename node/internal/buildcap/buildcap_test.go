package buildcap

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"topcoat/node/internal/buildsvc"
	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/servicebus"
)

type runner struct{ calls []string }

func (r *runner) Run(_ context.Context, dir string, _ []string, name string, args ...string) (string, error) {
	line := name + " " + strings.Join(args, " ")
	r.calls = append(r.calls, line)
	switch {
	case strings.HasPrefix(line, "git rev-parse"):
		return strings.Repeat("c", 40) + "\n", nil
	case strings.HasPrefix(line, "git clone --mirror"):
		_ = os.MkdirAll(args[len(args)-1], 0o750)
		_ = os.WriteFile(filepath.Join(args[len(args)-1], "HEAD"), []byte("x"), 0o600)
	}
	return "ok", nil
}

type host struct{}

func (host) Upload(context.Context, buildsvc.Upload) error { return nil }

// TestAStepGoesFromTheCapabilityOverTheSocketToTheServiceAndBack runs the real bus on a real
// Unix socket, the real service with a fake runner, and the capability the hub's call reaches.
func TestAStepGoesFromTheCapabilityOverTheSocketToTheServiceAndBack(t *testing.T) {
	dir, err := os.MkdirTemp("", "bc")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	bus, err := servicebus.New(dir, []servicebus.Spec{{ID: Service, Actions: buildsvc.Actions}},
		func(uid uint32) (string, bool) { return Service, int(uid) == os.Getuid() })
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	if err := bus.Listen(ctx, nil); err != nil {
		t.Fatal(err)
	}
	cl, err := servicebus.Dial(ctx, bus.SocketPath(Service), Service, buildsvc.Actions)
	if err != nil {
		t.Fatal(err)
	}
	r := &runner{}
	svc, err := buildsvc.New(buildsvc.Config{Work: t.TempDir(), Runner: r, Host: host{}, Sources: map[string]string{"release_repo": "/srv/r.git"}})
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = cl.Serve(ctx, svc.Handle) }()
	for i := 0; !bus.Connected(Service); i++ {
		if i > 300 {
			t.Skip("the kernel did not name the peer here (no SO_PEERCRED)")
		}
		<-timeAfter()
	}

	reg := capability.NewRegistry()
	if err := Register(reg, bus); err != nil {
		t.Fatal(err)
	}
	for _, name := range buildsvc.Actions {
		c, ok := reg.Get(name)
		if !ok {
			t.Fatalf("%s is not offered", name)
		}
		info := capability.InfoOf(c)
		if !info.Manage || info.ReadOnly || info.Scratch {
			t.Errorf("%s: %+v", name, info)
		}
		if !json.Valid(c.InputSchema()) {
			t.Errorf("%s: the schema is not JSON", name)
		}
	}
	fetch, _ := reg.Get("build.fetch")
	out, err := fetch.Invoke(ctx, json.RawMessage(`{"ref":"main","source":"release_repo"}`))
	if err != nil || !strings.Contains(string(out), strings.Repeat("c", 40)) {
		t.Fatalf("out=%s err=%v", out, err)
	}
	// a bad value never crosses the socket, and a good one the service refuses comes back as an error
	before := len(r.calls)
	if _, err := fetch.Invoke(ctx, json.RawMessage(`{"ref":"main; ls","source":"release_repo"}`)); err == nil {
		t.Fatal("a bad ref was accepted")
	}
	if len(r.calls) != before {
		t.Fatalf("a bad value reached the runner: %v", r.calls[before:])
	}
	if _, err := fetch.Invoke(ctx, json.RawMessage(`{"ref":"main","source":"dev_mirror"}`)); err == nil || !strings.Contains(err.Error(), "not configured") {
		t.Fatalf("got %v", err)
	}
}
