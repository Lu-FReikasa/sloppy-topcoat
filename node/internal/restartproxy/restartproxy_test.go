package restartproxy

import (
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strings"
	"sync"
	"testing"
)

// fakeDocker records every request that reaches it, so the tests can prove what was NOT forwarded.
type fakeDocker struct {
	mu   sync.Mutex
	seen []string
}

func (f *fakeDocker) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.seen = append(f.seen, r.Method+" "+r.URL.RequestURI())
	f.mu.Unlock()
	w.WriteHeader(http.StatusNoContent)
}

func setup(t *testing.T) (*httptest.Server, *fakeDocker) {
	t.Helper()
	fd := &fakeDocker{}
	up := httptest.NewServer(fd)
	t.Cleanup(up.Close)
	h, err := Handler("tcp://"+strings.TrimPrefix(up.URL, "http://"), nil)
	if err != nil {
		t.Fatal(err)
	}
	px := httptest.NewServer(h)
	t.Cleanup(px.Close)
	return px, fd
}

func do(t *testing.T, method, url, body string) int {
	t.Helper()
	req, _ := http.NewRequest(method, url, strings.NewReader(body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close() //nolint:errcheck
	_, _ = io.Copy(io.Discard, resp.Body)
	return resp.StatusCode
}

func TestOnlyARestartOfOneContainerIsForwarded(t *testing.T) {
	px, fd := setup(t)
	if c := do(t, "POST", px.URL+"/containers/web-1/restart?t=5", ""); c != 204 {
		t.Fatalf("restart: %d", c)
	}
	if c := do(t, "POST", px.URL+"/v1.45/containers/web-1/restart", ""); c != 204 {
		t.Fatalf("versioned restart: %d", c)
	}
	want := []string{"POST /containers/web-1/restart?t=5", "POST /containers/web-1/restart"}
	if len(fd.seen) != 2 || fd.seen[0] != want[0] || fd.seen[1] != want[1] {
		t.Fatalf("forwarded %v, want %v", fd.seen, want)
	}
}

// the rows of the manual checks and more: everything else is 403 and never reaches Docker
func TestEverythingElseIsRefusedAndNeverReachesDocker(t *testing.T) {
	px, fd := setup(t)
	cases := []struct{ method, path, body string }{
		{"POST", "/containers/create", "{}"},
		{"POST", "/containers/web-1/exec", "{}"},
		{"POST", "/containers/web-1/stop", ""},
		{"POST", "/containers/web-1/kill", ""},
		{"POST", "/containers/web-1/start", ""},
		{"POST", "/containers/web-1/pause", ""},
		{"POST", "/containers/web-1/update", "{}"},
		{"POST", "/containers/web-1/restart/extra", ""},
		{"POST", "/containers/web-1/../create", ""},
		{"POST", "/containers/web%2D1/restart", ""},
		{"POST", "/containers//restart", ""},
		{"POST", "/containers/-rf/restart", ""},
		{"POST", "/images/create?fromImage=x", ""},
		{"POST", "/build", ""},
		{"POST", "/volumes/create", "{}"},
		{"POST", "/networks/create", "{}"},
		{"POST", "/containers/web-1/restart?t=-1", ""},
		{"POST", "/containers/web-1/restart?t=99999", ""},
		{"POST", "/containers/web-1/restart?t=1&signal=SIGKILL", ""},
		{"POST", "/containers/web-1/restart?signal=SIGKILL", ""},
		{"POST", "/containers/web-1/restart", `{"x":1}`},
		{"GET", "/containers/json", ""},
		{"GET", "/containers/web-1/restart", ""},
		{"DELETE", "/containers/web-1", ""},
		{"PUT", "/containers/web-1/restart", ""},
		{"POST", "/v1.45/containers/web-1/stop", ""},
		{"POST", "/", ""},
	}
	for _, c := range cases {
		if got := do(t, c.method, px.URL+c.path, c.body); got != http.StatusForbidden {
			t.Errorf("%s %s: %d, want 403", c.method, c.path, got)
		}
	}
	if len(fd.seen) != 0 {
		t.Fatalf("reached Docker: %v", fd.seen)
	}
}

func TestOnlyTheContainerAndTheTimeoutAreCopiedUpstream(t *testing.T) {
	px, fd := setup(t)
	req, _ := http.NewRequest("POST", px.URL+"/containers/web-1/restart?t=3", nil)
	req.Header.Set("X-Evil", "1")
	req.Header.Set("Authorization", "Bearer x")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close() //nolint:errcheck
	if len(fd.seen) != 1 || fd.seen[0] != "POST /containers/web-1/restart?t=3" {
		t.Fatalf("%v", fd.seen)
	}
}

func TestServeRefusesANonLoopbackAddress(t *testing.T) {
	if err := Serve(t.Context(), "0.0.0.0:0", "unix:///nonexistent.sock", nil); err == nil || !strings.Contains(err.Error(), "loopback") {
		t.Fatalf("%v", err)
	}
	if err := Serve(t.Context(), "192.0.2.1:2376", "unix:///nonexistent.sock", nil); err == nil {
		t.Fatal("accepted a non-loopback address")
	}
}

func TestADockerThatIsNotThereIsAGatewayError(t *testing.T) {
	h, err := Handler("unix:///nonexistent.sock", nil)
	if err != nil {
		t.Fatal(err)
	}
	px := httptest.NewServer(h)
	defer px.Close()
	if c := do(t, "POST", px.URL+"/containers/web-1/restart", ""); c != http.StatusBadGateway {
		t.Fatalf("%d", c)
	}
}

func TestItReachesDockerOverAUnixSocket(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the Docker socket is a Linux thing")
	}
	sock := t.TempDir() + "/d.sock"
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Skip("no unix sockets here:", err)
	}
	fd := &fakeDocker{}
	srv := &http.Server{Handler: fd}
	go srv.Serve(ln) //nolint:errcheck
	t.Cleanup(func() { _ = srv.Close() })
	h, err := Handler("unix://"+sock, nil)
	if err != nil {
		t.Fatal(err)
	}
	px := httptest.NewServer(h)
	defer px.Close()
	if c := do(t, "POST", px.URL+"/containers/web-1/restart", ""); c != 204 || len(fd.seen) != 1 {
		t.Fatalf("%d %v", c, fd.seen)
	}
}
