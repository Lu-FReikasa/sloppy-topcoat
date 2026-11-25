package tools

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"topcoat/node/internal/providers/capability"
)

// fakeWriteProxy is a Docker write proxy: it records requests and answers restarts of known names.
type fakeWriteProxy struct {
	mu      sync.Mutex
	methods []string
	paths   []string
	srv     *httptest.Server
}

func newFakeWriteProxy(t *testing.T) *fakeWriteProxy {
	f := &fakeWriteProxy{}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.methods = append(f.methods, r.Method)
		f.paths = append(f.paths, r.URL.Path+"?"+r.URL.RawQuery)
		f.mu.Unlock()
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/containers/web/restart":
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodPost && r.URL.Path == "/containers/blocked/restart":
			http.Error(w, "forbidden by proxy", http.StatusForbidden)
		default:
			http.Error(w, `{"message":"No such container"}`, http.StatusNotFound)
		}
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeWriteProxy) writer(t *testing.T) *DockerWriter {
	w, err := NewDockerWriter("tcp://" + strings.TrimPrefix(f.srv.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	return w
}

func TestWriteCapabilitiesAreNotReadOnlyAndExistOnlyWhereTheConfigNamesWhatMayRestart(t *testing.T) {
	// nothing configured: neither is offered
	r := NewRegistry(Options{})
	for _, n := range []string{"docker.restart", "service.restart"} {
		if _, ok := r.Get(n); ok {
			t.Fatalf("%s is offered without a list", n)
		}
	}
	// a list without a write proxy: only the unit restart
	r = NewRegistry(Options{RestartContainers: []string{"web"}, RestartUnits: []string{"nginx.service"}})
	if _, ok := r.Get("docker.restart"); ok {
		t.Fatal("docker.restart without a write proxy")
	}
	c, ok := r.Get("service.restart")
	if !ok {
		t.Fatal("service.restart missing")
	}
	if capability.InfoOf(c).ReadOnly {
		t.Fatal("a restart must not be read-only")
	}
	// a write proxy without a list: nothing
	if _, ok := NewRegistry(Options{DockerWrite: newFakeWriteProxy(t).writer(t)}).Get("docker.restart"); ok {
		t.Fatal("docker.restart without a list")
	}
}

func TestDockerRestartRestartsOnlyAListedContainerAndReportsTheStateAfterwards(t *testing.T) {
	p := newFakeWriteProxy(t)
	d := dockerWithTwoContainers(t)
	o := Options{Docker: d.client(), DockerWrite: p.writer(t), RestartContainers: []string{"web", "blocked", "ghost"}}
	out, err := invoke(t, o, "docker.restart", `{"container":"web","timeout_seconds":5}`)
	if err != nil {
		t.Fatal(err)
	}
	if out["restarted"] != true || out["state"] != "running" || out["running"] != true {
		t.Fatalf("%v", out)
	}
	if len(p.paths) != 1 || p.methods[0] != "POST" || p.paths[0] != "/containers/web/restart?t=5" {
		t.Fatalf("%v %v", p.methods, p.paths)
	}
	// not on the list: refused before the proxy is asked
	if _, err := invoke(t, o, "docker.restart", `{"container":"db"}`); err == nil || !strings.Contains(err.Error(), "not on this node's list") {
		t.Fatalf("%v", err)
	}
	// the proxy's own refusal and an unknown container come back as errors
	if _, err := invoke(t, o, "docker.restart", `{"container":"blocked"}`); err == nil || !strings.Contains(err.Error(), "refuses") {
		t.Fatalf("%v", err)
	}
	if _, err := invoke(t, o, "docker.restart", `{"container":"ghost"}`); err == nil || !strings.Contains(err.Error(), "no such container") {
		t.Fatalf("%v", err)
	}
	// a name that is not a name never reaches the network
	n := len(p.paths)
	for _, bad := range []string{`{"container":"../web"}`, `{"container":"web/restart?x"}`, `{"container":"web","force":true}`, `{"container":"web","timeout_seconds":600}`} {
		if _, err := invoke(t, o, "docker.restart", bad); err == nil {
			t.Fatalf("accepted %s", bad)
		}
	}
	if len(p.paths) != n {
		t.Fatalf("a bad input reached the proxy: %v", p.paths)
	}
}

func TestTheDockerWriterCanDoNothingButRestart(t *testing.T) {
	p := newFakeWriteProxy(t)
	w := p.writer(t)
	if err := w.Restart(context.Background(), "web/../../images", 10); err == nil {
		t.Fatal("a path outside the restart endpoint was accepted")
	}
	if len(p.paths) != 0 {
		t.Fatalf("%v", p.paths)
	}
	if _, err := NewDockerWriter("tcp://10.0.0.5:2375"); err == nil {
		t.Fatal("a remote Docker write host was accepted")
	}
}

func TestServiceRestartRestartsOnlyAListedUnit(t *testing.T) {
	c := &fakeCommander{out: map[string]string{"systemctl show": "ActiveState=active\nSubState=running\nResult=success\n"}}
	o := Options{Commander: c, RestartUnits: []string{"nginx.service", "app-*.service"}}
	out, err := invoke(t, o, "service.restart", `{"unit":"app-api.service"}`)
	if err != nil {
		t.Fatal(err)
	}
	if out["restarted"] != true || out["activestate"] != "active" {
		t.Fatalf("%v", out)
	}
	if got := c.calls[0]; strings.Join(got, " ") != "systemctl restart --no-pager app-api.service" {
		t.Fatalf("%v", got)
	}
	n := c.called()
	for _, bad := range []string{`{"unit":"sshd.service"}`, `{"unit":"--now"}`, `{"unit":"nginx.service","force":true}`} {
		if _, err := invoke(t, o, "service.restart", bad); err == nil {
			t.Fatalf("accepted %s", bad)
		}
	}
	if c.called() != n {
		t.Fatalf("a refused unit ran a command: %v", c.calls)
	}
}
