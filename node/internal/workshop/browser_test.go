package workshop

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/capability"
)

// fakeBrowser stands in for the container: it records what it was asked and answers from a table.
type fakeBrowser struct {
	mu      sync.Mutex
	spec    BrowserSpec
	opens   int
	calls   []string
	args    []json.RawMessage
	closed  int
	openErr error
	answer  func(op string) (json.RawMessage, error)
}

func (f *fakeBrowser) Open(_ context.Context, s BrowserSpec) (BrowserSession, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.openErr != nil {
		return nil, f.openErr
	}
	f.spec = s
	f.opens++
	// the copy of the worktree exists while the session is open
	if _, err := os.Stat(s.Dir); err != nil {
		return nil, err
	}
	return &fakeSession{f: f}, nil
}

type fakeSession struct{ f *fakeBrowser }

func (s *fakeSession) Call(_ context.Context, op string, args json.RawMessage) (json.RawMessage, error) {
	s.f.mu.Lock()
	s.f.calls = append(s.f.calls, op)
	s.f.args = append(s.f.args, args)
	answer := s.f.answer
	s.f.mu.Unlock()
	if answer != nil {
		return answer(op)
	}
	return json.RawMessage(`{"ok":true,"op":"` + op + `"}`), nil
}

func (s *fakeSession) Close(context.Context) error {
	s.f.mu.Lock()
	s.f.closed++
	s.f.mu.Unlock()
	return nil
}

func (f *fakeBrowser) snapshotCalls() ([]string, int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...), f.closed
}

func browserFixture(t *testing.T, fb *fakeBrowser, mod func(*Options)) *fixture {
	t.Helper()
	f := newFixture(t, func(o *Options) {
		o.Browser = fb
		o.RunnerConfig = protocol.WorkshopRunner{Images: map[string]string{protocol.WorkshopBrowser: "browser:1"}}
		if mod != nil {
			mod(o)
		}
	})
	f.must(t, "repo.sync", map[string]any{"repo": "demo"})
	return f
}

func TestBrowserCapabilitiesNeedARunner(t *testing.T) {
	f := newFixture(t, nil)
	for _, c := range f.w.Registry() {
		if strings.HasPrefix(c.Name(), "browser.") {
			t.Fatalf("%s offered without a browser runner", c.Name())
		}
	}
}

func TestBrowserCapabilitiesAreScratchAndNotReadOnly(t *testing.T) {
	f := browserFixture(t, &fakeBrowser{}, nil)
	n := 0
	for _, c := range f.w.Registry() {
		if !strings.HasPrefix(c.Name(), "browser.") {
			continue
		}
		n++
		// scratch: the hub runs them for an admin without a click, and never for anybody else
		if i := capability.InfoOf(c); !i.Scratch || i.ReadOnly || i.Manage {
			t.Errorf("%s: %+v", c.Name(), i)
		}
	}
	if n != 9 {
		t.Fatalf("want 9 browser capabilities, got %d", n)
	}
}

func TestBrowserSessionFromOpenToClose(t *testing.T) {
	fb := &fakeBrowser{}
	f := browserFixture(t, fb, nil)
	open := f.must(t, "browser.open", map[string]any{"repo": "demo"})
	if open["mode"] != "built" || open["path"] != "/" || open["session"] == "" {
		t.Fatalf("open: %v", open)
	}
	if fb.spec.Image != "browser:1" || fb.spec.Mode != "built" || fb.spec.Timeout <= 0 {
		t.Fatalf("spec: %+v", fb.spec)
	}
	// the copy of the worktree has the files but no credentials (the same copy as repo.test)
	if _, err := os.Stat(filepath.Join(fb.spec.Dir, "app", "main.py")); err != nil {
		t.Fatalf("worktree copy: %v", err)
	}
	if _, err := os.Stat(filepath.Join(fb.spec.Dir, ".env")); err == nil {
		t.Fatal("the copy holds a credential file")
	}
	f.must(t, "browser.viewport", map[string]any{"width": 390, "height": 844, "scheme": "dark"})
	f.must(t, "browser.snapshot", map[string]any{"layout": true})
	f.must(t, "browser.act", map[string]any{"ref": "e12", "action": "click"})
	f.must(t, "browser.act", map[string]any{"action": "key", "value": "Tab"})
	f.must(t, "browser.audit", map[string]any{"checks": []string{"page_scroll"}, "matrix": []map[string]any{{"w": 320, "h": 640}}})
	f.must(t, "browser.animations", map[string]any{"trigger": map[string]any{"ref": "e3", "action": "hover"}, "sample_ms": 500})
	f.must(t, "browser.console", map[string]any{"since": 4})
	f.must(t, "browser.shot", map[string]any{"scale": 0.25})
	calls, closed := fb.snapshotCalls()
	want := []string{"viewport", "snapshot", "act", "act", "audit", "animations", "console", "shot"}
	if strings.Join(calls, ",") != strings.Join(want, ",") || closed != 0 {
		t.Fatalf("calls %v closed %d", calls, closed)
	}
	// defaults are filled in before they reach the helper
	var snap map[string]any
	_ = json.Unmarshal(fb.args[1], &snap)
	if snap["max_nodes"].(float64) != 200 || snap["layout"] != true {
		t.Fatalf("snapshot args: %s", fb.args[1])
	}
	run := fb.spec.Dir
	f.must(t, "browser.close", map[string]any{})
	if _, closed = fb.snapshotCalls(); closed != 1 {
		t.Fatalf("session closed %d times", closed)
	}
	if _, err := os.Stat(run); err == nil {
		t.Fatal("the files of the session were not removed")
	}
	_, err := f.call(t, "browser.snapshot", map[string]any{})
	refused(t, err, "no browser session is open")
}

func TestBrowserOneSessionPerNode(t *testing.T) {
	f := browserFixture(t, &fakeBrowser{}, nil)
	f.must(t, "browser.open", map[string]any{"repo": "demo"})
	_, err := f.call(t, "browser.open", map[string]any{"repo": "demo"})
	refused(t, err, "a browser session is open")
	f.must(t, "browser.close", map[string]any{})
	f.must(t, "browser.open", map[string]any{"repo": "demo", "mode": "dev", "path": "/chat"})
}

func TestBrowserInputsAreChecked(t *testing.T) {
	f := browserFixture(t, &fakeBrowser{}, nil)
	f.must(t, "browser.open", map[string]any{"repo": "demo"})
	cases := []struct {
		name string
		in   map[string]any
		want string
	}{
		{"browser.viewport", map[string]any{"width": 100, "height": 800}, "width must be"},
		{"browser.viewport", map[string]any{"width": 800, "height": 9000}, "height must be"},
		{"browser.viewport", map[string]any{"width": 800, "height": 800, "dpr": 5.0}, "dpr must be"},
		{"browser.viewport", map[string]any{"width": 800, "height": 800, "extra": 1}, "invalid input"},
		{"browser.snapshot", map[string]any{"scope": "#main"}, "must be a ref"},
		{"browser.snapshot", map[string]any{"max_nodes": 5000}, "max_nodes must be"},
		{"browser.act", map[string]any{"action": "click"}, "must be a ref"},
		{"browser.act", map[string]any{"action": "click", "ref": "javascript:alert(1)"}, "must be a ref"},
		{"browser.act", map[string]any{"action": "type", "ref": "e1"}, "needs a value"},
		{"browser.act", map[string]any{"action": "scroll", "value": "sideways"}, "value must be one of"},
		{"browser.act", map[string]any{"action": "eval", "ref": "e1", "value": "x"}, "action must be one of"},
		{"browser.audit", map[string]any{"checks": []string{"everything"}}, "checks must be one of"},
		{"browser.audit", map[string]any{"matrix": []map[string]any{{"w": 10, "h": 600}}}, "matrix w must be"},
		{"browser.animations", map[string]any{"sample_ms": 10}, "sample_ms must be"},
		{"browser.animations", map[string]any{"properties": []string{"filter"}}, "properties must be one of"},
		{"browser.animations", map[string]any{"trigger": map[string]any{"ref": "e1", "action": "drag"}}, "trigger.action must be"},
		{"browser.console", map[string]any{"since": -1}, "since must not be negative"},
		{"browser.shot", map[string]any{"scale": 3.0}, "scale must be"},
		{"browser.shot", map[string]any{"filmstrip": 40}, "filmstrip must be"},
	}
	for _, c := range cases {
		_, err := f.call(t, c.name, c.in)
		if err == nil {
			t.Errorf("%s %v: accepted", c.name, c.in)
			continue
		}
		refused(t, err, c.want)
	}
}

func TestBrowserOpenChecksModeAndPath(t *testing.T) {
	f := browserFixture(t, &fakeBrowser{}, nil)
	for _, in := range []map[string]any{
		{"repo": "demo", "mode": "prod"},
		{"repo": "demo", "path": "chat"},
		{"repo": "demo", "path": "/../etc/passwd"},
		{"repo": "demo", "path": "http://example.invalid/"},
		{"repo": "missing"},
	} {
		if _, err := f.call(t, "browser.open", in); err == nil {
			t.Errorf("%v: accepted", in)
		}
	}
}

func TestBrowserOpenPutsTheThemeIntoTheMailbox(t *testing.T) {
	fb := &fakeBrowser{}
	f := browserFixture(t, fb, nil)
	theme := map[string]any{"key": "abend", "name": "Abend", "css": "--accent: #f60;"}
	open := f.must(t, "browser.open", map[string]any{"repo": "demo", "theme": theme})
	if open["theme"] != true {
		t.Fatalf("open: %v", open)
	}
	raw, err := os.ReadFile(filepath.Join(fb.spec.NodeBoxDir, "theme.json"))
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil || got["key"] != "abend" || got["css"] != "--accent: #f60;" {
		t.Fatalf("theme.json: %s", raw)
	}
}

func TestBrowserMailboxIsWritableForTheHelper(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("file modes")
	}
	fb := &fakeBrowser{}
	f := browserFixture(t, fb, nil)
	f.must(t, "browser.open", map[string]any{"repo": "demo"})
	for _, d := range []string{"in", "out"} {
		st, err := os.Stat(filepath.Join(fb.spec.NodeBoxDir, d))
		if err != nil {
			t.Fatal(err)
		}
		if st.Mode().Perm() != 0o777 {
			t.Errorf("%s: %v (the helper's uid must remove commands and write answers)", d, st.Mode().Perm())
		}
	}
}

func TestBrowserOpenRefusesABadTheme(t *testing.T) {
	f := browserFixture(t, &fakeBrowser{}, nil)
	for _, theme := range []any{
		map[string]any{"name": "no key"},
		map[string]any{"key": "Bad Key"},
		map[string]any{"key": "big", "css": strings.Repeat("x", 70<<10)},
		"a string",
	} {
		if _, err := f.call(t, "browser.open", map[string]any{"repo": "demo", "theme": theme}); err == nil {
			t.Errorf("%v: accepted", theme)
		}
	}
}

func TestBrowserOpenNeedsAnImage(t *testing.T) {
	f := newFixture(t, func(o *Options) { o.Browser = &fakeBrowser{} })
	f.must(t, "repo.sync", map[string]any{"repo": "demo"})
	_, err := f.call(t, "browser.open", map[string]any{"repo": "demo"})
	refused(t, err, "no browser image")
}

func TestBrowserOpenFailureLeavesNothingBehind(t *testing.T) {
	fb := &fakeBrowser{openErr: errors.New("docker is down")}
	f := browserFixture(t, fb, nil)
	if _, err := f.call(t, "browser.open", map[string]any{"repo": "demo"}); err == nil || !strings.Contains(err.Error(), "could not start") {
		t.Fatalf("err: %v", err)
	}
	if entries, _ := os.ReadDir(filepath.Join(f.w.dir, ".runs")); len(entries) != 0 {
		t.Fatalf("left behind: %v", entries)
	}
	fb.mu.Lock()
	fb.openErr = nil
	fb.mu.Unlock()
	f.must(t, "browser.open", map[string]any{"repo": "demo"}) // a failed open does not block the next
}

func TestBrowserAnswersAreBounded(t *testing.T) {
	fb := &fakeBrowser{answer: func(string) (json.RawMessage, error) {
		return json.RawMessage(`{"tree":"` + strings.Repeat("x", browserAnswerBytes) + `"}`), nil
	}}
	f := browserFixture(t, fb, nil)
	f.must(t, "browser.open", map[string]any{"repo": "demo"})
	_, err := f.call(t, "browser.snapshot", map[string]any{})
	refused(t, err, "ask for less")
}

func TestBrowserMasksSecretsInAnswers(t *testing.T) {
	fb := &fakeBrowser{answer: func(string) (json.RawMessage, error) {
		return json.RawMessage(`{"text":"key sk-live-0123456789abcdef0123456789 end"}`), nil
	}}
	f := browserFixture(t, fb, nil)
	f.must(t, "browser.open", map[string]any{"repo": "demo"})
	m := f.must(t, "browser.console", map[string]any{})
	if strings.Contains(m["text"].(string), "sk-live-0123456789") {
		t.Fatalf("a secret left the node: %v", m)
	}
}

func TestBrowserHelperErrorsReachTheCaller(t *testing.T) {
	fb := &fakeBrowser{answer: func(string) (json.RawMessage, error) { return nil, inputErr("no element has the ref e99") }}
	f := browserFixture(t, fb, nil)
	f.must(t, "browser.open", map[string]any{"repo": "demo"})
	_, err := f.call(t, "browser.act", map[string]any{"ref": "e99", "action": "click"})
	refused(t, err, "no element has the ref e99")
}

func TestBrowserIdleSessionEnds(t *testing.T) {
	fb := &fakeBrowser{}
	f := browserFixture(t, fb, func(o *Options) { o.BrowserIdle = 80 * time.Millisecond })
	f.must(t, "browser.open", map[string]any{"repo": "demo"})
	deadline := time.Now().Add(3 * time.Second)
	for {
		if _, closed := fb.snapshotCalls(); closed == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the idle session was not ended")
		}
		time.Sleep(20 * time.Millisecond)
	}
	_, err := f.call(t, "browser.snapshot", map[string]any{})
	refused(t, err, "no browser session is open")
}

func TestBrowserUseKeepsTheSessionAlive(t *testing.T) {
	fb := &fakeBrowser{}
	f := browserFixture(t, fb, func(o *Options) { o.BrowserIdle = 400 * time.Millisecond })
	f.must(t, "browser.open", map[string]any{"repo": "demo"})
	for i := 0; i < 6; i++ {
		time.Sleep(100 * time.Millisecond)
		f.must(t, "browser.console", map[string]any{})
	}
	if _, closed := fb.snapshotCalls(); closed != 0 {
		t.Fatal("a session in use was ended")
	}
	f.must(t, "browser.close", map[string]any{})
}

// --- the Docker session ----------------------------------------------------------------------------

// helperLoop plays the helper inside the container: it announces itself and answers every command in
// the mailbox with a canned result.
func helperLoop(t *testing.T, box string, stop <-chan struct{}) {
	t.Helper()
	go func() {
		_ = os.WriteFile(filepath.Join(box, "out", "ready.json"), []byte(`{"ready":true}`), 0o644)
		for {
			select {
			case <-stop:
				return
			case <-time.After(10 * time.Millisecond):
			}
			files, _ := filepath.Glob(filepath.Join(box, "in", "*.json"))
			for _, p := range files {
				base := strings.TrimSuffix(filepath.Base(p), ".json")
				b, _ := os.ReadFile(p)
				_ = os.Remove(p)
				if base == "close" {
					continue
				}
				var msg struct {
					ID   string          `json:"id"`
					Op   string          `json:"op"`
					Args json.RawMessage `json:"args"`
				}
				_ = json.Unmarshal(b, &msg)
				out := `{"ok":true,"result":{"op":"` + msg.Op + `"}}`
				if msg.Op == "act" {
					out = `{"ok":false,"error":"no element has the ref e9"}`
				}
				_ = os.WriteFile(filepath.Join(box, "out", msg.ID+".json"), []byte(out), 0o644)
			}
		}
	}()
}

func TestDockerBrowserAsksForASealedContainerAndTalksThroughTheMailbox(t *testing.T) {
	f := &fakeDocker{}
	r := runnerFor(t, f)
	root := t.TempDir()
	box, work := filepath.Join(root, "box"), filepath.Join(root, "work")
	for _, d := range []string{filepath.Join(box, "in"), filepath.Join(box, "out"), work} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	stop := make(chan struct{})
	defer close(stop)
	helperLoop(t, box, stop)
	sess, err := r.Open(context.Background(), BrowserSpec{
		Image: "browser:1", Dir: work, BoxDir: box, NodeBoxDir: box, Mode: "built", Path: "/",
		MemoryMB: 1024, CPUMilli: 1000, Timeout: 10 * time.Minute,
	})
	if err != nil {
		t.Fatal(err)
	}
	c := f.created
	host := c["HostConfig"].(map[string]any)
	if c["Image"] != "browser:1" || c["NetworkDisabled"] != true || host["NetworkMode"] != "none" ||
		host["ReadonlyRootfs"] != true || host["Privileged"] != false || host["PidsLimit"] == nil {
		t.Fatalf("container: %v", c)
	}
	if caps := host["CapDrop"].([]any); len(caps) != 1 || caps[0] != "ALL" {
		t.Fatalf("capabilities: %v", caps)
	}
	binds := host["Binds"].([]any)
	if len(binds) != 2 || !strings.HasSuffix(binds[0].(string), ":/work:ro") || !strings.HasSuffix(binds[1].(string), ":/box:rw") {
		t.Fatalf("binds: %v", binds)
	}
	cmd := c["Cmd"].([]any)
	if cmd[0] != "node" || !strings.Contains(strings.Join(anyStrings(cmd), " "), "--mode built") {
		t.Fatalf("command: %v", cmd)
	}
	out, err := sess.Call(context.Background(), "snapshot", json.RawMessage(`{"max_nodes":10}`))
	if err != nil || string(out) != `{"op":"snapshot"}` {
		t.Fatalf("snapshot: %s %v", out, err)
	}
	_, err = sess.Call(context.Background(), "act", json.RawMessage(`{}`))
	refused(t, err, "no element has the ref e9")
	if err := sess.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	joined := strings.Join(f.requests, "\n")
	if !strings.Contains(joined, "/kill") || !strings.Contains(joined, "DELETE /containers/abc123") {
		t.Fatalf("the container was not ended and removed: %s", joined)
	}
}

func TestDockerBrowserTimesOutWhenTheHelperIsSilent(t *testing.T) {
	f := &fakeDocker{}
	r := runnerFor(t, f)
	root := t.TempDir()
	box := filepath.Join(root, "box")
	for _, d := range []string{filepath.Join(box, "in"), filepath.Join(box, "out")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	_, err := r.Open(ctx, BrowserSpec{Image: "browser:1", Dir: root, BoxDir: box, NodeBoxDir: box, Mode: "built", Path: "/", MemoryMB: 512, CPUMilli: 500, Timeout: time.Minute})
	if err == nil || !strings.Contains(err.Error(), "did not come up") {
		t.Fatalf("err: %v", err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if !strings.Contains(strings.Join(f.requests, "\n"), "DELETE /containers/abc123") {
		t.Fatal("the container of a browser that did not come up was not removed")
	}
}

func TestDockerBrowserImageMissing(t *testing.T) {
	f := &fakeDocker{status: 404}
	r := runnerFor(t, f)
	_, err := r.Open(context.Background(), BrowserSpec{Image: "browser:1", Dir: t.TempDir(), BoxDir: t.TempDir(), Mode: "built", Path: "/", MemoryMB: 512, CPUMilli: 500, Timeout: time.Minute})
	if err == nil || !strings.Contains(err.Error(), "pull it once by hand") {
		t.Fatalf("err: %v", err)
	}
}

func anyStrings(in []any) []string {
	out := make([]string, len(in))
	for i, v := range in {
		out[i], _ = v.(string)
	}
	return out
}

func TestDockerBrowserReportsAHelperThatCouldNotStart(t *testing.T) {
	f := &fakeDocker{}
	r := runnerFor(t, f)
	box := t.TempDir()
	for _, d := range []string{filepath.Join(box, "in"), filepath.Join(box, "out")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	msg := `{"ready":false,"error":"the worktree has no built GUI (web/dist/index.html)"}`
	if err := os.WriteFile(filepath.Join(box, "out", "ready.json"), []byte(msg), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := r.Open(context.Background(), BrowserSpec{Image: "browser:1", Dir: box, BoxDir: box, NodeBoxDir: box, Mode: "built", Path: "/", MemoryMB: 512, CPUMilli: 500, Timeout: time.Minute})
	if err == nil || !strings.Contains(err.Error(), "no built GUI") {
		t.Fatalf("err: %v", err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if !strings.Contains(strings.Join(f.requests, "\n"), "DELETE /containers/abc123") {
		t.Fatal("the container was not removed")
	}
}

func TestBrowserImageKeyIsPartOfTheRunnerConfig(t *testing.T) {
	base := func(image string) protocol.NodeConfig {
		w := protocol.WorkshopConfig{
			Dir: "/var/lib/topcoat-node/workshop", AllowedHosts: []string{"github.com"},
			Runner: &protocol.WorkshopRunner{Type: "docker", Images: map[string]string{protocol.WorkshopBrowser: image}},
		}
		return protocol.NodeConfig{Roles: []string{"dev"}, Runner: protocol.RunnerConfig{Type: protocol.RunnerNone}, Tools: &protocol.ToolsConfig{Workshop: &w}}
	}
	good := base("topcoat-workshop-browser:1")
	if err := good.Validate(); err != nil {
		t.Fatalf("the browser image was refused: %v", err)
	}
	bad := base("x; rm -rf /")
	if err := bad.Validate(); err == nil {
		t.Fatal("a bad image reference was accepted")
	}
}
