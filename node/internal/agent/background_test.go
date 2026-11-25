package agent

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/fake"
	"topcoat/node/internal/providers/watcher"
)

const bgConfig = `{"roles":["gpu"],"runner":{"type":"fake"},` +
	`"allowed_actions":["models.list","models.load","models.unload","models.download","models.delete","node.benchmark"],` +
	`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,` +
	`"watcher":{"grace_seconds":0,"cooldown_seconds":0,"rules":[{"process":"game.exe"}]},"capabilities":[]}`

func laptopProfile() fake.Profile {
	p := fake.DefaultProfile()
	p.CPU = fake.ProfileCPU{Model: "i7-1255U", Cores: 10, Threads: 12, PerformanceCores: 2, EfficiencyCores: 8}
	p.GPUs = []fake.ProfileGPU{{Name: "Iris Xe", Vendor: "intel", VRAMTotalMB: 128, VRAMBaseUsedMB: 50, Integrated: true, SharedTotalMB: 16000}}
	return p
}

func TestDownloadDeleteOverSession(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.world.DownloadStep = 0
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, bgConfig)

	res := hc.action(t, "models.download", `{"source":{"type":"huggingface","repo":"Qwen/Qwen2.5-1.5B-Instruct-GGUF","file":"qwen2.5-1.5b-instruct-q4_k_m.gguf"}}`)
	if !res.OK || res.Result.(map[string]any)["model"] != "qwen2.5-1.5b-instruct-q4_k_m" {
		t.Fatalf("%+v %+v", res, res.Error)
	}
	for {
		d := hc.waitEvent(t, protocol.EventDownloadProgress)
		if d["status"] == "done" {
			if d["downloaded_bytes"] != d["total_bytes"] || d["total_bytes"].(float64) <= 0 {
				t.Fatalf("%v", d)
			}
			break
		}
		if d["status"] != "running" {
			t.Fatalf("%v", d)
		}
	}
	inv := hc.waitType(t, protocol.TypeInventory)
	if !strings.Contains(string(inv.Body), "qwen2.5-1.5b-instruct-q4_k_m") {
		t.Fatal("inventory lacks the downloaded model")
	}
	// Same name again is rejected.
	res = hc.action(t, "models.download", `{"source":{"type":"huggingface","repo":"a/b","file":"qwen2.5-1.5b-instruct-q4_k_m.gguf"}}`)
	if res.OK || res.Error.Code != protocol.ErrInvalidArgs {
		t.Fatalf("%+v", res.Error)
	}
	// Delete requires confirm; a loaded model is reported unloaded.
	if res := hc.action(t, "models.delete", `{"model":"qwen2.5-1.5b-instruct-q4_k_m"}`); res.OK || res.Error.Code != protocol.ErrInvalidArgs {
		t.Fatalf("delete without confirm: %+v", res)
	}
	if res := hc.action(t, "models.load", `{"model":"qwen2.5-1.5b-instruct-q4_k_m","ttl_seconds":60}`); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	if res := hc.action(t, "models.delete", `{"model":"qwen2.5-1.5b-instruct-q4_k_m","confirm":true}`); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	if d := hc.waitEvent(t, protocol.EventModelUnloaded); d["reason"] != "deleted" {
		t.Fatalf("%v", d)
	}
	if res := hc.action(t, "models.delete", `{"model":"nope","confirm":true}`); res.OK || res.Error.Code != protocol.ErrInvalidArgs {
		t.Fatalf("%+v", res)
	}
}

func TestBenchmarkIGPUSweepOverSession(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), laptopProfile())
	h.world.BenchDelay = 0
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, bgConfig)

	for _, bad := range []string{`{}`, `{"models":[]}`, `{"models":["nope"]}`, `{"models":["fake-llm-3b-q8"],"quick":"yes"}`, `{"models":["fake-llm-3b-q8","fake-llm-3b-q8"]}`} {
		if res := hc.action(t, "node.benchmark", bad); res.OK || res.Error.Code != protocol.ErrInvalidArgs {
			t.Fatalf("%s: %+v", bad, res)
		}
	}
	res := hc.action(t, "node.benchmark", `{"models":["fake-llm-3b-q8"]}`)
	if !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	id := res.Result.(map[string]any)["benchmark_id"]
	if d := hc.waitEvent(t, protocol.EventBenchmarkStarted); d["benchmark_id"] != id {
		t.Fatalf("%v", d)
	}
	backends := map[string]int{}
	for i := 0; i < 5; i++ { // vulkan + CPU sweep 2/6/10/12
		d := hc.waitEvent(t, protocol.EventBenchmarkResult)
		backends[d["backend"].(string)]++
	}
	if backends["vulkan"] != 1 || backends["cpu"] != 4 {
		t.Fatalf("%v", backends)
	}
	d := hc.waitEvent(t, protocol.EventBenchmarkDone)
	if d["ok"] != true || d["recommended_threads"] != float64(2) {
		t.Fatalf("%v", d)
	}
}

func TestBenchmarkAbortedByGameAndBusy(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), laptopProfile())
	h.world.BenchDelay = 200 * time.Millisecond
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, bgConfig)

	if res := hc.action(t, "node.benchmark", `{"models":["fake-llm-3b-q8"],"quick":true}`); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	hc.waitEvent(t, protocol.EventBenchmarkStarted)
	if res := hc.action(t, "node.benchmark", `{"models":["fake-llm-3b-q8"]}`); res.OK || res.Error.Code != protocol.ErrBusy {
		t.Fatalf("second benchmark: %+v", res)
	}
	h.watcher.Start("game.exe")
	d := hc.waitEvent(t, protocol.EventBenchmarkDone)
	if d["ok"] != false || !strings.Contains(d["error"].(string), "aborted: game detected") {
		t.Fatalf("%v", d)
	}
	if res := hc.action(t, "node.benchmark", `{"models":["fake-llm-3b-q8"]}`); res.OK || res.Error.Code != protocol.ErrBusy {
		t.Fatalf("benchmark while gaming: %+v", res)
	}
}

func TestCooldownAfterGame(t *testing.T) {
	// A fake clock with one timer per After call: a cooldown that was canceled by a new game
	// can never swallow the tick meant for the later one (a single shared channel could).
	var mu sync.Mutex
	var asked []time.Duration
	var timers []chan time.Time
	h := newHarness(t, downURL(t), t.TempDir(), fake.DefaultProfile(), func(o *Options) {
		o.After = func(d time.Duration) <-chan time.Time {
			ch := make(chan time.Time, 1)
			mu.Lock()
			asked = append(asked, d)
			timers = append(timers, ch)
			mu.Unlock()
			return ch
		}
	})
	a := h.agent
	applyTestConfig(t, a, func(c *protocol.NodeConfig) {
		c.Watcher.CooldownSeconds = nil // default 60 s
		c.Watcher.Rules = append(c.Watcher.Rules, protocol.WatcherRule{PathPrefix: `D:\Steam\common`})
	})
	ctx := context.Background()
	a.refreshState(ctx)
	steam := watcher.Event{Process: "foo.exe", Path: `D:\Steam\common\Foo\foo.exe`, Started: true}
	a.handleProcessEvent(ctx, steam)
	if a.State() != protocol.StateBusyGaming {
		t.Fatal("not busy")
	}
	steam.Started = false
	a.handleProcessEvent(ctx, steam)
	if a.State() != protocol.StateBusyGaming {
		t.Fatal("cooldown must keep the node busy")
	}
	// A game during the cooldown cancels it; ending that game starts a new one.
	a.handleProcessEvent(ctx, watcher.Event{Process: "game.exe", Started: true})
	a.handleProcessEvent(ctx, watcher.Event{Process: "game.exe", Started: false})
	// Both cooldown goroutines have asked for their timer (After is evaluated before the
	// select), so the ticks below reach the right ones.
	eventually(t, "two cooldown timers", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(timers) == 2
	})
	mu.Lock()
	first, second := timers[0], timers[1]
	mu.Unlock()
	first <- time.Now() // the canceled cooldown fires late: it must change nothing
	second <- time.Now()
	eventually(t, "available after cooldown", func() bool { return a.refreshState(ctx) == protocol.StateAvailable })
	a.bg.Wait()
	mu.Lock()
	defer mu.Unlock()
	if len(asked) != 2 || asked[0] != 60*time.Second || asked[1] != 60*time.Second {
		t.Fatalf("cooldowns asked %v", asked)
	}
	got := strings.Join(queuedKinds(t, a), ",")
	if !strings.HasPrefix(got, "game_detected:,state_changed:,game_ended:,game_detected:,game_ended:") || !strings.HasSuffix(got, "state_changed:") {
		t.Fatalf("queued %s", got)
	}
}

func TestBenchmarkGoesOnAfterAFailingBackend(t *testing.T) {
	// Vulkan runs out of device memory; the CPU sweep still runs and the benchmark ends ok,
	// with the failure in its own event (E2-05 A1).
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), laptopProfile())
	h.world.BenchDelay = 0
	h.world.BenchFailGPU = true
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, bgConfig)
	if res := hc.action(t, "node.benchmark", `{"models":["fake-llm-3b-q8"],"quick":true}`); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	failure := hc.waitEvent(t, protocol.EventBenchmarkError)
	if failure["backend"] != "vulkan" || !strings.Contains(failure["error"].(string), "Device memory allocation failed") {
		t.Fatalf("%v", failure)
	}
	if _, has := failure["n_gpu_layers"]; has {
		t.Fatalf("a fit run has no fixed layer count: %v", failure)
	}
	for i := 0; i < 2; i++ { // quick: the first and the last CPU thread count
		if d := hc.waitEvent(t, protocol.EventBenchmarkResult); d["backend"] != "cpu" {
			t.Fatalf("%v", d)
		}
	}
	d := hc.waitEvent(t, protocol.EventBenchmarkDone)
	if d["ok"] != true || d["failed_runs"] != float64(1) {
		t.Fatalf("%v", d)
	}
}

func TestABenchmarkThatTakesTooLongIsEnded(t *testing.T) {
	old := benchTimeout
	benchTimeout = 150 * time.Millisecond
	t.Cleanup(func() { benchTimeout = old })
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), laptopProfile())
	h.world.BenchDelay = 10 * time.Second
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, bgConfig)

	if res := hc.action(t, "node.benchmark", `{"models":["fake-llm-3b-q8"]}`); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	hc.waitEvent(t, protocol.EventBenchmarkStarted)
	d := hc.waitEvent(t, protocol.EventBenchmarkDone)
	if d["ok"] != false || !strings.Contains(d["error"].(string), "timeout") {
		t.Fatalf("%v", d)
	}
	// the place is free again: a new benchmark starts
	h.world.BenchDelay = 0
	if res := hc.action(t, "node.benchmark", `{"models":["fake-llm-3b-q8"],"quick":true}`); !res.OK {
		t.Fatalf("after the timeout: %+v", res)
	}
}
