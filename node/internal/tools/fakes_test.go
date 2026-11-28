package tools

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeCommander answers journalctl / systemctl from a map and records every call.
type fakeCommander struct {
	mu    sync.Mutex
	calls [][]string
	out   map[string]string // first argument ("journalctl", "systemctl show", …) -> output
	err   error
}

func (f *fakeCommander) Run(_ context.Context, bin string, args ...string) ([]byte, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, append([]string{bin}, args...))
	if f.err != nil {
		return nil, f.err
	}
	key := bin
	if len(args) > 0 && (bin == "systemctl") {
		key += " " + args[0]
	}
	return []byte(f.out[key]), nil
}

func (f *fakeCommander) called() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.calls)
}

// fakeDocker is a Docker API on the loopback that records every request.
type fakeDocker struct {
	t       *testing.T
	srv     *httptest.Server
	mu      sync.Mutex
	methods []string
	paths   []string
	queries []string
	list    string            // /containers/json
	inspect map[string]string // ref -> /containers/<ref>/json
	logs    map[string][]byte // ref -> raw log stream
}

func newFakeDocker(t *testing.T) *fakeDocker {
	f := &fakeDocker{t: t, inspect: map[string]string{}, logs: map[string][]byte{}}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.methods = append(f.methods, r.Method)
		f.paths = append(f.paths, r.URL.Path)
		f.queries = append(f.queries, r.URL.RawQuery)
		f.mu.Unlock()
		switch {
		case r.URL.Path == "/containers/json":
			_, _ = w.Write([]byte(f.list))
		case strings.HasSuffix(r.URL.Path, "/json"):
			ref := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/containers/"), "/json")
			if s, ok := f.inspect[ref]; ok {
				_, _ = w.Write([]byte(s))
				return
			}
			http.Error(w, `{"message":"No such container"}`, http.StatusNotFound)
		case strings.HasSuffix(r.URL.Path, "/logs"):
			ref := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/containers/"), "/logs")
			if b, ok := f.logs[ref]; ok {
				_, _ = w.Write(b)
				return
			}
			http.Error(w, `{"message":"No such container"}`, http.StatusNotFound)
		default:
			http.Error(w, "not found", http.StatusNotFound)
		}
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeDocker) client() *Docker {
	d, err := NewDocker("tcp://" + strings.TrimPrefix(f.srv.URL, "http://"))
	if err != nil {
		f.t.Fatal(err)
	}
	return d
}

// frames builds a multiplexed log stream (stdout = 1, stderr = 2).
func frames(parts ...[2]string) []byte {
	var b []byte
	for _, p := range parts {
		stream := byte(1)
		if p[0] == "err" {
			stream = 2
		}
		h := make([]byte, 8)
		h[0] = stream
		binary.BigEndian.PutUint32(h[4:], uint32(len(p[1])))
		b = append(append(b, h...), p[1]...)
	}
	return b
}

// invoke runs a capability with the URL of a web call vouched for as the person's (url_origin
// "person"), so the tests of everything else need no search first; invokeAs tests the origin rule.
func invoke(t *testing.T, o Options, name, input string) (map[string]any, error) {
	t.Helper()
	return invokeAs(WithPersonURL(context.Background()), t, o, name, input)
}

func invokeAs(parent context.Context, t *testing.T, o Options, name, input string) (map[string]any, error) {
	t.Helper()
	r := NewRegistry(o)
	c, ok := r.Get(name)
	if !ok {
		t.Fatalf("no capability %s", name)
	}
	ctx, cancel := context.WithTimeout(parent, 5*time.Second)
	defer cancel()
	raw, err := c.Invoke(ctx, json.RawMessage(input))
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("output is not a JSON object: %s", raw)
	}
	return out, nil
}

func mustTime(s string) time.Time {
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		panic(err)
	}
	return t
}

func mustJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return string(b)
}
