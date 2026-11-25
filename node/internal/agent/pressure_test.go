package agent

import (
	"context"
	"encoding/json"
	"sync"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/fake"
	"topcoat/node/internal/providers/runner"
)

// pressureConfig keeps fake-llm-8b-q4 resident; short timings for the test.
const pressureConfig = `{"roles":["gpu"],"runner":{"type":"fake"},` +
	`"allowed_actions":["models.list","models.load","models.unload"],` +
	`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,` +
	`"watcher":{"grace_seconds":0,"cooldown_seconds":0,"rules":[{"process":"game.exe"}]},"capabilities":[],` +
	`"residents":[{"model":"fake-llm-8b-q4","priority":80}],` +
	`"pressure":{"sustain_seconds":1,"reload_after_seconds":1,"min_off_seconds":0}}`

func pressureProfile() fake.Profile {
	p := fake.DefaultProfile()
	p.GPUs[0].VRAMTotalMB = 12288 // 12 GB: a 6 GB foreign allocation does not fit next to the model
	return p
}

func heartbeatState(t *testing.T, e protocol.Envelope) (protocol.Heartbeat, bool) {
	t.Helper()
	if e.Type != protocol.TypeHeartbeat {
		return protocol.Heartbeat{}, false
	}
	var hb protocol.Heartbeat
	if err := e.DecodeBody(&hb); err != nil {
		t.Fatal(err)
	}
	return hb, true
}

// The integration test from the E1-06 brief: a foreign 6 GB allocation makes
// the resident model yield within sustain + unload time; when it is gone the
// model is loaded again after reload_after_seconds.
func TestPressureYieldsAndReloadsResident(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), pressureProfile(), func(o *Options) {
		o.PressurePoll = 50 * time.Millisecond
	})
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, pressureConfig)

	loaded := hc.waitEvent(t, protocol.EventModelLoaded) // the resident comes on its own
	if loaded["model"] != "fake-llm-8b-q4" || loaded["reason"] != "resident" {
		t.Fatalf("model_loaded %v", loaded)
	}

	if err := h.world.SetOther(0, fake.Other{ForeignVRAMMB: 6000, ForeignUtil: 95}); err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	unloaded := hc.waitEvent(t, protocol.EventModelUnloaded)
	if unloaded["model"] != "fake-llm-8b-q4" || unloaded["reason"] != "vram_pressure" {
		t.Fatalf("model_unloaded %v", unloaded)
	}
	if took := time.Since(start); took > 3*time.Second {
		t.Fatalf("yield took %v (sustain is 1 s)", took)
	}
	env := hc.waitFor(t, "heartbeat yielded", func(e protocol.Envelope) bool {
		hb, ok := heartbeatState(t, e)
		return ok && hb.State == protocol.StateYielded
	})
	hb, _ := heartbeatState(t, env)
	if g := hb.GPUs[0]; g.ForeignVRAMUsedMB == nil || *g.ForeignVRAMUsedMB < 6000 || g.ForeignGPUUtil == nil || *g.ForeignGPUUtil != 95 {
		t.Fatalf("foreign usage in heartbeat: %+v", g)
	}
	// the heartbeat also carries the utilization of the whole GPU (at least the other program's share)
	if g := hb.GPUs[0]; g.GPUUtil == nil || *g.GPUUtil < 95 || *g.GPUUtil > 100 {
		t.Fatalf("gpu_util in heartbeat: %+v", g)
	}
	if names := loadedNames(t, h.world); len(names) != 0 {
		t.Fatalf("still loaded: %v", names)
	}

	// The other program uses the GPU a lot even after freeing memory: no reload yet.
	if err := h.world.SetOther(0, fake.Other{ForeignUtil: 95}); err != nil {
		t.Fatal(err)
	}
	time.Sleep(1500 * time.Millisecond)
	if names := loadedNames(t, h.world); len(names) != 0 {
		t.Fatalf("reloaded while the GPU is busy: %v", names)
	}
	if err := h.world.SetOther(0, fake.Other{}); err != nil {
		t.Fatal(err)
	}
	reloaded := hc.waitEvent(t, protocol.EventModelLoaded)
	if reloaded["model"] != "fake-llm-8b-q4" || reloaded["reason"] != "pressure_cleared" {
		t.Fatalf("model_loaded %v", reloaded)
	}
	hc.waitFor(t, "heartbeat available", func(e protocol.Envelope) bool {
		hb, ok := heartbeatState(t, e)
		return ok && hb.State == protocol.StateAvailable
	})
}

// A watcher rule vetoes reloads while the game runs, even with free VRAM.
func TestRuleVetoesReload(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), func(o *Options) {
		o.PressurePoll = 50 * time.Millisecond
	})
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, pressureConfig)
	hc.waitEvent(t, protocol.EventModelLoaded)

	h.watcher.Start("game.exe")
	if data := hc.waitEvent(t, protocol.EventModelUnloaded); data["reason"] != "game" {
		t.Fatalf("unload reason %v", data)
	}
	time.Sleep(1500 * time.Millisecond) // reload_after is 1 s: a reload would have happened
	if names := loadedNames(t, h.world); len(names) != 0 {
		t.Fatalf("reloaded during the game: %v", names)
	}
	h.watcher.Stop("game.exe")
	if data := hc.waitEvent(t, protocol.EventModelLoaded); data["reason"] != "resident" {
		t.Fatalf("reload reason %v", data)
	}
}

// Residents with auto_reload are exempt from the idle TTL.
func TestResidentsAreExemptFromIdleUnload(t *testing.T) {
	var mu sync.Mutex
	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	h := newHarness(t, downURL(t), t.TempDir(), fake.DefaultProfile(), func(o *Options) {
		o.Now = func() time.Time { mu.Lock(); defer mu.Unlock(); return now }
	})
	cfg := `{"roles":["gpu"],"runner":{"type":"fake"},"allowed_actions":[],"heartbeat_interval_seconds":10,` +
		`"idle_ttl_seconds":60,"watcher":{"rules":[]},"capabilities":[],` +
		`"residents":[{"model":"fake-llm-8b-q4"},{"model":"fake-llm-3b-q8","auto_reload":false}]}`
	c, err := protocol.ParseNodeConfig([]byte(cfg))
	if err != nil {
		t.Fatal(err)
	}
	h.agent.applyConfig(c, "hash")
	ctx := context.Background()
	for _, m := range []string{"fake-llm-8b-q4", "fake-llm-3b-q8"} {
		if _, err := h.world.Runner().Load(ctx, m, runner.LoadOptions{}); err != nil {
			t.Fatal(err)
		}
	}
	h.agent.checkIdle(ctx) // first seen: idle timers start
	mu.Lock()
	now = now.Add(2 * time.Minute)
	mu.Unlock()
	h.agent.checkIdle(ctx)
	names := loadedNames(t, h.world)
	if len(names) != 1 || names[0] != "fake-llm-8b-q4" {
		t.Fatalf("loaded after idle check: %v", names)
	}
}

// Manual loads lift the flap guard; manual unloads are reported to the controller.
func TestManualActionsReachTheController(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	hc := hub.accept(t)
	cfg := `{"roles":["gpu"],"runner":{"type":"fake"},"allowed_actions":["models.load","models.unload"],` +
		`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[],` +
		`"residents":[{"model":"fake-llm-3b-q8"}]}`
	hc.handshake(t, cfg)
	now := time.Now()
	for i := 0; i < 3; i++ {
		h.agent.press.Yielded("fake-llm-3b-q8", now)
	}
	res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`)
	if !res.OK {
		t.Fatalf("load: %+v", res)
	}
	if res := hc.action(t, "models.unload", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("unload: %+v", res)
	}
	if h.agent.press.Active(now) {
		t.Fatal("nothing should wait for a reload after a manual unload")
	}
	b, _ := json.Marshal(res)
	t.Logf("%s", b)
}

// Test 1 on node-amd: LM Studio got 4.8 GB of VRAM plus 7.7 GB of shared memory
// while 4.6 GB stayed free. The unmet-demand signal yields our resident.
func TestUnmetDemandYieldsResident(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), func(o *Options) {
		o.PressurePoll = 50 * time.Millisecond
	})
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, pressureConfig)
	hc.waitEvent(t, protocol.EventModelLoaded)

	if err := h.world.SetOther(0, fake.Other{ForeignVRAMMB: 4844, ForeignSharedMB: 7743}); err != nil {
		t.Fatal(err)
	}
	unloaded := hc.waitEvent(t, protocol.EventModelUnloaded)
	if unloaded["reason"] != "vram_pressure" || unloaded["signal"] != "unmet_demand" {
		t.Fatalf("model_unloaded %v", unloaded)
	}
	env := hc.waitFor(t, "heartbeat yielded", func(e protocol.Envelope) bool {
		hb, ok := heartbeatState(t, e)
		return ok && hb.State == protocol.StateYielded
	})
	hb, _ := heartbeatState(t, env)
	if g := hb.GPUs[0]; g.ForeignSharedMaxMB == nil || *g.ForeignSharedMaxMB != 7743 {
		t.Fatalf("heartbeat gpu %+v", g)
	}
	// 10 GB of VRAM are free now, but LM Studio still waits in shared memory.
	time.Sleep(1500 * time.Millisecond)
	if names := loadedNames(t, h.world); len(names) != 0 {
		t.Fatalf("reloaded while another program waits for VRAM: %v", names)
	}
	if err := h.world.SetOther(0, fake.Other{}); err != nil {
		t.Fatal(err)
	}
	if data := hc.waitEvent(t, protocol.EventModelLoaded); data["reason"] != "pressure_cleared" {
		t.Fatalf("model_loaded %v", data)
	}
}
