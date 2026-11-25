package workshop

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeDocker struct {
	mu       sync.Mutex
	requests []string
	created  map[string]any
	wait     func(w http.ResponseWriter, r *http.Request)
	status   int             // status of create; 0 = 201
	logs     func() []string // the log lines; nil = two fixed lines
}

func (f *fakeDocker) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.requests = append(f.requests, r.Method+" "+r.URL.Path)
	f.mu.Unlock()
	switch {
	case r.Method == "POST" && r.URL.Path == "/containers/create":
		if f.status != 0 {
			w.WriteHeader(f.status)
			_, _ = w.Write([]byte(`{"message":"No such image: builder:1"}`))
			return
		}
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &f.created)
		w.WriteHeader(201)
		_, _ = w.Write([]byte(`{"Id":"abc123"}`))
	case strings.HasSuffix(r.URL.Path, "/start"):
		w.WriteHeader(204)
	case strings.HasSuffix(r.URL.Path, "/wait"):
		if f.wait != nil {
			f.wait(w, r)
			return
		}
		_, _ = w.Write([]byte(`{"StatusCode":0}`))
	case strings.HasSuffix(r.URL.Path, "/kill"):
		w.WriteHeader(204)
	case strings.HasSuffix(r.URL.Path, "/logs"):
		lines := []string{"ok one\n", "ok two\n"}
		if f.logs != nil {
			lines = f.logs()
		}
		for _, line := range lines {
			hdr := make([]byte, 8)
			hdr[0] = 1
			binary.BigEndian.PutUint32(hdr[4:], uint32(len(line)))
			_, _ = w.Write(append(hdr, line...))
		}
	case r.Method == "DELETE":
		w.WriteHeader(204)
	default:
		w.WriteHeader(404)
	}
}

func runnerFor(t *testing.T, f *fakeDocker) *DockerRunner {
	t.Helper()
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	r, err := NewDockerRunner("tcp://" + strings.TrimPrefix(srv.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func job(t *testing.T) Job {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "a.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	return Job{Template: "make-check", Image: "builder:1", Cmd: []string{"make", "check"}, Dir: dir, MemoryMB: 512, CPUMilli: 1500, Timeout: 5 * time.Second}
}

func TestDockerRunnerAsksForASealedContainer(t *testing.T) {
	f := &fakeDocker{}
	res, err := runnerFor(t, f).Run(context.Background(), job(t))
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode != 0 || res.Output != "ok one\nok two\n" || res.TimedOut {
		t.Fatalf("result: %+v", res)
	}
	c := f.created
	host := c["HostConfig"].(map[string]any)
	if c["Image"] != "builder:1" || c["NetworkDisabled"] != true || host["NetworkMode"] != "none" ||
		host["ReadonlyRootfs"] != true || host["Privileged"] != false || host["Memory"].(float64) != 512<<20 ||
		host["MemorySwap"] != host["Memory"] || host["NanoCpus"].(float64) != 1.5e9 {
		t.Fatalf("container: %v", c)
	}
	if caps := host["CapDrop"].([]any); len(caps) != 1 || caps[0] != "ALL" {
		t.Fatalf("capabilities: %v", caps)
	}
	if binds := host["Binds"].([]any); len(binds) != 1 || !strings.HasSuffix(binds[0].(string), ":/work:rw") {
		t.Fatalf("binds: %v", binds)
	}
	env := strings.Join(toStrings(c["Env"]), " ")
	if env != "HOME=/tmp CI=1" {
		t.Fatalf("environment: %s", env)
	}
	if user := c["User"].(string); strings.HasPrefix(user, "0:") {
		t.Fatalf("runs as root: %s", user)
	}
	// the container is removed at the end
	if last := f.requests[len(f.requests)-1]; last != "DELETE /containers/abc123" {
		t.Fatalf("requests: %v", f.requests)
	}
}

func TestDockerRunnerUsesTheNetworkOnlyWhenTold(t *testing.T) {
	f := &fakeDocker{}
	j := job(t)
	j.Network = "pkg-net"
	if _, err := runnerFor(t, f).Run(context.Background(), j); err != nil {
		t.Fatal(err)
	}
	if f.created["HostConfig"].(map[string]any)["NetworkMode"] != "pkg-net" || f.created["NetworkDisabled"] != false {
		t.Fatalf("container: %v", f.created)
	}
}

func TestDockerRunnerKillsARunThatTakesTooLong(t *testing.T) {
	f := &fakeDocker{wait: func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() }}
	j := job(t)
	j.Timeout = 200 * time.Millisecond
	res, err := runnerFor(t, f).Run(context.Background(), j)
	if err != nil {
		t.Fatal(err)
	}
	if !res.TimedOut || res.ExitCode != 137 {
		t.Fatalf("result: %+v", res)
	}
	joined := strings.Join(f.requests, ",")
	if !strings.Contains(joined, "POST /containers/abc123/kill") || !strings.HasSuffix(joined, "DELETE /containers/abc123") {
		t.Fatalf("requests: %v", f.requests)
	}
}

func TestDockerRunnerReportsAFailingTestAndAMissingImage(t *testing.T) {
	f := &fakeDocker{wait: func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(`{"StatusCode":2}`)) }}
	res, err := runnerFor(t, f).Run(context.Background(), job(t))
	if err != nil || res.ExitCode != 2 {
		t.Fatalf("result: %+v %v", res, err)
	}
	_, err = runnerFor(t, &fakeDocker{status: 404}).Run(context.Background(), job(t))
	if err == nil || !strings.Contains(err.Error(), "pull it once by hand") {
		t.Fatalf("missing image: %v", err)
	}
}

func TestDockerHostMustBeLocal(t *testing.T) {
	for _, h := range []string{"tcp://192.168.1.5:2375", "http://x", "unix://"} {
		if _, err := NewDockerRunner(h); err == nil {
			t.Errorf("%s was accepted", h)
		}
	}
	if _, err := NewDockerRunner("unix:///run/ws.sock"); err != nil {
		t.Error(err)
	}
}

func TestDockerRunnerEndsAQuietRunButNotOneThatKeepsTalking(t *testing.T) {
	// quiet: the log never changes, the run is ended after the idle limit, long before the hard one
	f := &fakeDocker{wait: func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() }}
	rn := runnerFor(t, f)
	rn.PollEvery = 10 * time.Millisecond
	j := job(t)
	j.Timeout, j.IdleTimeout = 5*time.Second, 150*time.Millisecond
	start := time.Now()
	res, err := rn.Run(context.Background(), j)
	if err != nil {
		t.Fatal(err)
	}
	if !res.TimedOutIdle || res.TimedOut || res.ExitCode != 137 || time.Since(start) > 3*time.Second {
		t.Fatalf("result: %+v after %s", res, time.Since(start))
	}
	// talking: every look shows a new line, so the idle limit never hits; the hard limit does
	var n int
	var mu sync.Mutex
	g := &fakeDocker{
		wait: func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() },
		logs: func() []string {
			mu.Lock()
			defer mu.Unlock()
			n++
			return []string{"line " + strings.Repeat("x", n) + "\n"}
		},
	}
	rn = runnerFor(t, g)
	rn.PollEvery = 10 * time.Millisecond
	var seen []string
	j = job(t)
	j.Timeout, j.IdleTimeout = 400*time.Millisecond, 100*time.Millisecond
	j.OnOutput = func(tail string) { seen = append(seen, tail) }
	res, err = rn.Run(context.Background(), j)
	if err != nil {
		t.Fatal(err)
	}
	if res.TimedOutIdle || !res.TimedOut || len(seen) < 5 {
		t.Fatalf("result: %+v, %d looks", res, len(seen))
	}
}

func TestDockerRunnerStopsWhenTheCallerCancels(t *testing.T) {
	f := &fakeDocker{wait: func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() }}
	rn := runnerFor(t, f)
	ctx, cancel := context.WithCancel(context.Background())
	go func() { time.Sleep(100 * time.Millisecond); cancel() }()
	_, err := rn.Run(ctx, job(t))
	if err == nil {
		t.Fatal("a canceled run reported no error")
	}
	joined := strings.Join(f.requests, ",")
	if !strings.Contains(joined, "POST /containers/abc123/kill") || !strings.HasSuffix(joined, "DELETE /containers/abc123") {
		t.Fatalf("requests: %v", f.requests)
	}
}

func TestTheStartAndTheEndOfALongOutputAreKept(t *testing.T) {
	head := "1 failed: E   AssertionError: got the word\n"
	long := head + strings.Repeat("web check line\n", 5000) + "web: done\n"
	got := keepEnds(long, outputHeadBytes, outputTailBytes)
	if !strings.HasPrefix(got, head) || !strings.HasSuffix(got, "web: done\n") {
		t.Fatalf("the ends were not kept: %q … %q", got[:60], got[len(got)-30:])
	}
	if len(got) > testOutputBytes || !strings.Contains(got, "left out") {
		t.Fatalf("not cut as expected: %d bytes", len(got))
	}
	if keepEnds("short", 10, 10) != "short" {
		t.Fatal("a short output was changed")
	}
}

// openUp makes the worktree copy writable for the unprivileged test user, and a symbolic link in it
// does not carry the chmod to a file outside the copy.
func TestOpenUpOpensTheCopyButNotWhatALinkPointsTo(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix modes")
	}
	base := t.TempDir()
	dir, outside := filepath.Join(base, "copy"), filepath.Join(base, "outside.txt")
	if err := os.MkdirAll(filepath.Join(dir, "sub"), 0o700); err != nil {
		t.Fatal(err)
	}
	for name, mode := range map[string]os.FileMode{"sub/plain.txt": 0o600, "run.sh": 0o700} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("x"), mode); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(outside, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(dir, "link")); err != nil {
		t.Fatal(err)
	}
	if err := openUp(dir); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]os.FileMode{".": 0o777, "sub": 0o777, "sub/plain.txt": 0o666, "run.sh": 0o777} {
		st, err := os.Stat(filepath.Join(dir, name))
		if err != nil {
			t.Fatal(err)
		}
		if got := st.Mode().Perm(); got != want {
			t.Errorf("%s: mode %o, want %o", name, got, want)
		}
	}
	if st, err := os.Stat(outside); err != nil || st.Mode().Perm() != 0o600 {
		t.Errorf("the file behind the link was changed: %v %v", st.Mode().Perm(), err)
	}
}
