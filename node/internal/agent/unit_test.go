package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"topcoat/node/internal/diskfree"
	"topcoat/node/internal/llamacpp"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/fake"
	"topcoat/node/internal/providers/runner"
	"topcoat/node/internal/providers/watcher"
	"topcoat/node/internal/schematest"
)

type fakeClock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

func applyTestConfig(t *testing.T, a *Agent, mutate func(*protocol.NodeConfig)) {
	t.Helper()
	cfg, err := protocol.ParseNodeConfig([]byte(testConfig))
	if err != nil {
		t.Fatal(err)
	}
	if mutate != nil {
		mutate(&cfg)
	}
	a.applyConfig(cfg, "")
}

func queuedKinds(t *testing.T, a *Agent) []string {
	t.Helper()
	var out []string
	for _, e := range a.queue.Snapshot() {
		raw, _ := json.Marshal(e)
		if err := schematest.Validate("event", raw); err != nil {
			t.Errorf("queued event violates schema: %v", err)
		}
		var ev protocol.Event
		_ = e.DecodeBody(&ev)
		out = append(out, ev.Kind+":"+stringOf(ev.Data["reason"]))
	}
	return out
}

func stringOf(v any) string {
	s, _ := v.(string)
	return s
}

func execAction(t *testing.T, a *Agent, name, args string) (any, *protocol.Error) {
	t.Helper()
	var act protocol.Action
	if err := json.Unmarshal([]byte(`{"name":"`+name+`","args":`+args+`}`), &act); err != nil {
		t.Fatal(err)
	}
	res, perr, follow := a.execute(context.Background(), act)
	if follow != nil {
		follow()
	}
	return res, perr
}

func TestIdleUnload(t *testing.T) {
	clock := &fakeClock{now: time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)}
	h := newHarness(t, downURL(t), t.TempDir(), fake.DefaultProfile(), func(o *Options) { o.Now = clock.Now })
	a := h.agent
	applyTestConfig(t, a, func(c *protocol.NodeConfig) { c.IdleTTLSeconds = 60 })
	ctx := context.Background()

	if _, perr := execAction(t, a, "models.load", `{"model":"fake-llm-8b-q4"}`); perr != nil {
		t.Fatal(perr)
	}
	// Loaded outside the agent (e.g. directly in the runner UI): timer starts when first seen.
	if _, err := h.world.Runner().Load(ctx, "fake-llm-3b-q8", runner.LoadOptions{}); err != nil {
		t.Fatal(err)
	}
	a.checkIdle(ctx)

	clock.Advance(30 * time.Second)
	a.checkIdle(ctx)
	if n := loadedNames(t, h.world); len(n) != 2 {
		t.Fatalf("unloaded too early: %v", n)
	}
	clock.Advance(31 * time.Second)
	a.checkIdle(ctx)
	if n := loadedNames(t, h.world); len(n) != 0 {
		t.Fatalf("idle models still loaded: %v", n)
	}
	got := strings.Join(queuedKinds(t, a), ",")
	if got != "model_loaded:,model_unloaded:idle,model_unloaded:idle" {
		t.Fatalf("queued %s", got)
	}

	// Disabled TTL never unloads.
	applyTestConfig(t, a, func(c *protocol.NodeConfig) { c.IdleTTLSeconds = 0 })
	if _, perr := execAction(t, a, "models.load", `{"model":"fake-llm-8b-q4"}`); perr != nil {
		t.Fatal(perr)
	}
	clock.Advance(24 * time.Hour)
	a.checkIdle(ctx)
	if n := loadedNames(t, h.world); len(n) != 1 {
		t.Fatalf("ttl 0 unloaded: %v", n)
	}
}

func TestGameGracePeriod(t *testing.T) {
	grace := make(chan time.Time)
	var graceAsked time.Duration
	var mu sync.Mutex
	profile := fake.DefaultProfile()
	profile.Loaded = []string{"fake-llm-8b-q4"}
	h := newHarness(t, downURL(t), t.TempDir(), profile, func(o *Options) {
		o.After = func(d time.Duration) <-chan time.Time {
			mu.Lock()
			graceAsked = d
			mu.Unlock()
			return grace
		}
	})
	a := h.agent
	applyTestConfig(t, a, func(c *protocol.NodeConfig) { c.Watcher.GraceSeconds = 5 })
	ctx := context.Background()
	a.refreshState(ctx)

	// Game starts: busy immediately, unload only after the grace period.
	a.handleProcessEvent(ctx, watcher.Event{Process: "game.exe", Started: true})
	if a.State() != protocol.StateBusyGaming {
		t.Fatalf("state %s", a.State())
	}
	if n := loadedNames(t, h.world); len(n) != 1 {
		t.Fatalf("unloaded before grace: %v", n)
	}
	// Duplicate start is ignored.
	a.handleProcessEvent(ctx, watcher.Event{Process: "GAME.EXE", Started: true})
	grace <- time.Now()
	a.bg.Wait() // unload goroutine finished (events emitted)
	if n := loadedNames(t, h.world); len(n) != 0 {
		t.Fatalf("still loaded after grace: %v", n)
	}
	mu.Lock()
	if graceAsked != 5*time.Second {
		t.Fatalf("grace %v", graceAsked)
	}
	mu.Unlock()
	a.handleProcessEvent(ctx, watcher.Event{Process: "game.exe", Started: false})
	if a.State() != protocol.StateAvailable {
		t.Fatalf("state after game %s", a.State())
	}

	// Game that ends within the grace period: nothing is unloaded.
	if _, perr := execAction(t, a, "models.load", `{"model":"fake-llm-3b-q8"}`); perr != nil {
		t.Fatal(perr)
	}
	a.handleProcessEvent(ctx, watcher.Event{Process: "game.exe", Started: true})
	a.handleProcessEvent(ctx, watcher.Event{Process: "game.exe", Started: false})
	a.bg.Wait()
	if n := loadedNames(t, h.world); len(n) != 1 {
		t.Fatalf("short game unloaded models: %v", n)
	}
	got := strings.Join(queuedKinds(t, a), ",")
	want := "game_detected:,state_changed:,model_unloaded:game,game_ended:,state_changed:," +
		"model_loaded:,game_detected:,state_changed:,game_ended:,state_changed:"
	if got != want {
		t.Fatalf("queued\n got %s\nwant %s", got, want)
	}
}

func TestMultipleGamesAndRuleChange(t *testing.T) {
	h := newHarness(t, downURL(t), t.TempDir(), fake.DefaultProfile())
	a := h.agent
	applyTestConfig(t, a, nil)
	ctx := context.Background()
	a.refreshState(ctx)
	a.handleProcessEvent(ctx, watcher.Event{Process: "a.exe", Started: true})
	a.handleProcessEvent(ctx, watcher.Event{Process: "b.exe", Started: true})
	a.handleProcessEvent(ctx, watcher.Event{Process: "a.exe", Started: false})
	if a.State() != protocol.StateBusyGaming {
		t.Fatal("still one game running")
	}
	// Rule for b.exe removed from config -> treated as ended.
	a.endGamesNotMatching(ctx, []watcher.Rule{{Process: "a.exe"}})
	if a.State() != protocol.StateAvailable {
		t.Fatalf("state %s", a.State())
	}
	// Ending an unknown process is a no-op.
	a.handleProcessEvent(ctx, watcher.Event{Process: "zzz.exe", Started: false})
	a.bg.Wait()
}

func TestDegradedWhenRunnerUnavailable(t *testing.T) {
	h := newHarness(t, downURL(t), t.TempDir(), fake.DefaultProfile())
	a := h.agent
	ctx := context.Background()
	// No config yet: no runner -> degraded.
	if s := a.refreshState(ctx); s != protocol.StateDegraded {
		t.Fatalf("state without config %s", s)
	}
	applyTestConfig(t, a, nil)
	if s := a.refreshState(ctx); s != protocol.StateAvailable {
		t.Fatalf("state %s", s)
	}
	h.world.SetRunnerAvailable(false)
	if s := a.refreshState(ctx); s != protocol.StateDegraded {
		t.Fatalf("state %s", s)
	}
	env, err := a.heartbeat(ctx)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(env)
	if err := schematest.Validate("heartbeat", raw); err != nil {
		t.Fatal(err)
	}
	inv, err := a.inventory(ctx)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ = json.Marshal(inv)
	if err := schematest.Validate("inventory", raw); err != nil {
		t.Fatal(err)
	}
	var body protocol.Inventory
	_ = inv.DecodeBody(&body)
	if body.Runner.Available || body.Runner.Detail == "" {
		t.Fatalf("runner info %+v", body.Runner)
	}
	// Start-up (no runner) -> available is silent; available -> degraded is reported.
	if got := strings.Join(queuedKinds(t, a), ","); got != "state_changed:" {
		t.Fatalf("queued %s", got)
	}
	if _, perr := execAction(t, a, "models.load", `{"model":"fake-llm-3b-q8"}`); perr == nil || perr.Code != protocol.ErrRunnerError {
		t.Fatalf("load on degraded runner: %+v", perr)
	}
}

func TestRuntimeNotInstalledIsDegraded(t *testing.T) {
	data := t.TempDir()
	h := newHarness(t, downURL(t), data, fake.DefaultProfile(), func(o *Options) {
		o.NewRunner = func(c protocol.RunnerConfig) runner.ModelRunner {
			return runner.NewLlamaServer(runner.LlamaServerOptions{
				DataDir: data, ModelDirs: c.ModelDirs, Backend: "cpu", Release: llamacpp.Pinned().Default,
				Installer:     &llamacpp.Installer{Root: filepath.Join(data, "runtime"), Manifest: llamacpp.Pinned(), BaseURL: "http://127.0.0.1:1"},
				RetryInterval: time.Hour,
			})
		}
	})
	a := h.agent
	applyTestConfig(t, a, func(c *protocol.NodeConfig) {
		c.Runner = protocol.RunnerConfig{Type: "llamaserver", ModelDirs: []string{"/models"}}
	})
	t.Cleanup(a.closeRunner)
	ctx := context.Background()
	if s := a.refreshState(ctx); s != protocol.StateDegraded {
		t.Fatalf("state %s", s)
	}
	inv, err := a.inventory(ctx)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(inv)
	if err := schematest.Validate("inventory", raw); err != nil {
		t.Fatal(err)
	}
	var body protocol.Inventory
	_ = inv.DecodeBody(&body)
	if body.Runner.Available || body.Runner.Backend != "cpu" || body.Runner.Release == "" || body.Runner.Detail == "" {
		t.Fatalf("%+v", body.Runner)
	}
	if _, perr := execAction(t, a, "models.load", `{"model":"m"}`); perr == nil || perr.Code != protocol.ErrRunnerError {
		t.Fatalf("load without runtime: %+v", perr)
	}
}

func TestNotAllowedWithoutConfig(t *testing.T) {
	h := newHarness(t, downURL(t), t.TempDir(), fake.DefaultProfile())
	if _, perr := execAction(t, h.agent, "models.list", `{}`); perr == nil || perr.Code != protocol.ErrNotAllowed {
		t.Fatalf("got %+v", perr)
	}
}

// resourceRunner wraps a runner with what the llama-server runner adds in E2-05: a model
// directory, a download pre-check and a load that can run out of RAM.
type resourceRunner struct {
	runner.ModelRunner
	dir       string
	checkErr  error
	loadErr   error
	downloads int
	// download replaces the wrapped runner's download (nil = use it): the fake's takes as long as
	// the world's fake clock lets it, which a test that only asks "did a download start?" must not wait for
	download func(context.Context, runner.DownloadRequest) (runner.Model, error)
}

func (r *resourceRunner) ModelDir() string { return r.dir }
func (r *resourceRunner) CheckDownload(context.Context, runner.DownloadRequest) error {
	return r.checkErr
}
func (r *resourceRunner) Load(ctx context.Context, name string, o runner.LoadOptions) (runner.LoadedModel, error) {
	if r.loadErr != nil {
		return runner.LoadedModel{}, r.loadErr
	}
	return r.ModelRunner.Load(ctx, name, o)
}
func (r *resourceRunner) Download(ctx context.Context, req runner.DownloadRequest) (runner.Model, error) {
	r.downloads++
	if r.download != nil {
		return r.download(ctx, req)
	}
	return r.ModelRunner.Download(ctx, req)
}

func TestResourceGuardsAreReportedWithTheirOwnErrorCodes(t *testing.T) {
	rr := &resourceRunner{dir: "/models", download: func(context.Context, runner.DownloadRequest) (runner.Model, error) {
		return runner.Model{Name: "Big", SizeBytes: 1}, nil
	}}
	var inner runner.ModelRunner
	h := newHarness(t, downURL(t), t.TempDir(), fake.DefaultProfile(), func(o *Options) {
		inner = o.NewRunner(protocol.RunnerConfig{Type: protocol.RunnerFake})
		rr.ModelRunner = inner
		o.NewRunner = func(protocol.RunnerConfig) runner.ModelRunner { return rr }
		o.DiskFree = func(path string) (diskfree.Usage, error) {
			if path != "/models" {
				t.Errorf("measured %q, not the model directory", path)
			}
			return diskfree.Usage{TotalBytes: 500 << 30, FreeBytes: 120 << 30}, nil
		}
	})
	a := h.agent
	// the download that is not refused (below) is a background job of the agent; it writes into
	// the test's directory until it has finished. Cleanups run last-in-first-out, so this one
	// waits before the temporary directory is removed.
	t.Cleanup(a.bg.Wait)
	applyTestConfig(t, a, nil)

	// the inventory carries the space of the model drive and passes the schema
	inv, err := a.inventory(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(inv)
	if err := schematest.Validate("inventory", raw); err != nil {
		t.Fatal(err)
	}
	var body protocol.Inventory
	_ = inv.DecodeBody(&body)
	if body.Hardware.DiskTotalMB == nil || *body.Hardware.DiskTotalMB != 500*1024 || *body.Hardware.DiskFreeMB != 120*1024 {
		t.Fatalf("%+v", body.Hardware)
	}

	// a download that does not fit is refused before anything starts
	rr.checkErr = fmt.Errorf("%w: needs 20000 MB", runner.ErrInsufficientDisk)
	args := `{"source":{"type":"huggingface","repo":"org/repo","file":"Big.gguf"}}`
	if _, perr := execAction(t, a, "models.download", args); perr == nil || perr.Code != protocol.ErrInsufficientDisk {
		t.Fatalf("got %+v", perr)
	}
	if rr.downloads != 0 {
		t.Fatal("a refused download must not start")
	}
	// a check that could not be made (Hugging Face down) does not block
	rr.checkErr = errors.New("hugging face metadata: connection refused")
	if _, perr := execAction(t, a, "models.download", args); perr != nil {
		t.Fatalf("an unavailable check must not refuse: %+v", perr)
	}

	// a load that would not fit into the RAM has its own code
	rr.loadErr = fmt.Errorf("load: %w: not enough free RAM", runner.ErrInsufficientMemory)
	if _, perr := execAction(t, a, "models.load", `{"model":"fake-llm-3b-q8"}`); perr == nil || perr.Code != protocol.ErrInsufficientMemory {
		t.Fatalf("got %+v", perr)
	}
	rr.loadErr = errors.New("exec format error")
	if _, perr := execAction(t, a, "models.load", `{"model":"fake-llm-3b-q8"}`); perr == nil || perr.Code != protocol.ErrRunnerError {
		t.Fatalf("other errors stay runner_error: %+v", perr)
	}
}
