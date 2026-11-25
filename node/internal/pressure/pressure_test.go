package pressure

import (
	"testing"
	"time"

	"topcoat/node/internal/protocol"
)

var t0 = time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)

func defaults() protocol.Pressure {
	c := protocol.NodeConfig{Pressure: &protocol.PressureConfig{}}
	p := c.EffectivePressure()
	p.RAMEnabled = false
	return p
}

func ptr[T any](v T) *T { return &v }

func resident(model string, prio int, reload bool) protocol.Resident {
	return protocol.Resident{Model: model, Priority: ptr(prio), AutoReload: ptr(reload)}
}

// rig is a synthetic machine: one 16 GB GPU, a desktop using some VRAM, our
// loaded models and "other programs".
type rig struct {
	total, desktop, foreign int64
	loaded                  []Model
	util                    float64
	utilKnown               bool
	demotedMB               int64 // our memory paged to shared
	sharedMeasured          bool
	foreignShared           int64 // largest shared use of one other program
	foreignSharedKnown      bool
	noGPU                   bool
	ramTotal, ramUsed       int64
	onDisk                  map[string]int64
	rule, self, down        bool
}

func newRig() *rig {
	return &rig{total: 16384, desktop: 800, sharedMeasured: true, utilKnown: true,
		onDisk: map[string]int64{"a": 5000, "b": 4000, "c": 3000}}
}

func (r *rig) load(name string, vram int64) {
	r.loaded = append(r.loaded, Model{Name: name, VRAMMB: vram})
}

func (r *rig) unload(name string) {
	out := r.loaded[:0:0]
	for _, m := range r.loaded {
		if m.Name != name {
			out = append(out, m)
		}
	}
	r.loaded = out
}

func (r *rig) sample(now time.Time) Sample {
	var own int64
	for _, m := range r.loaded {
		if !m.CPU {
			own += m.VRAMMB
		}
	}
	s := Sample{Now: now, Loaded: append([]Model(nil), r.loaded...), OnDisk: r.onDisk,
		RAMTotalMB: r.ramTotal, RAMUsedMB: r.ramUsed, RuleActive: r.rule, SelfActivity: r.self, RunnerDown: r.down}
	if !r.noGPU {
		ownDed := own - r.demotedMB
		s.GPUs = []GPU{{
			TotalMB: r.total, UsedMB: r.desktop + r.foreign + ownDed,
			OwnMB: ownDed, OwnMeasured: true,
			OwnSharedMB: r.demotedMB, SharedMeasured: r.sharedMeasured,
			ForeignUtil: r.util, UtilKnown: r.utilKnown,
			ForeignSharedMaxMB: r.foreignShared, ForeignSharedKnown: r.foreignSharedKnown,
		}}
	}
	return s
}

func step(t *testing.T, c *Controller, r *rig, at time.Duration) *Action {
	t.Helper()
	return c.Step(r.sample(t0.Add(at)))
}

// apply executes an action on the rig like the agent would.
func apply(t *testing.T, c *Controller, r *rig, a *Action, at time.Duration) {
	t.Helper()
	switch a.Kind {
	case Yield:
		r.unload(a.Model)
		c.Yielded(a.Model, t0.Add(at))
	case Reload:
		vram := r.onDisk[a.Model]
		r.load(a.Model, vram)
		c.Reloaded(a.Model, vram, 0)
	}
}

func wantYield(t *testing.T, a *Action, model, reason string) {
	t.Helper()
	if a == nil || a.Kind != Yield || a.Model != model || a.Reason != reason {
		t.Fatalf("want yield %s (%s), got %+v", model, reason, a)
	}
}

func wantReload(t *testing.T, a *Action, model, reason string) {
	t.Helper()
	if a == nil || a.Kind != Reload || a.Model != model || a.Reason != reason {
		t.Fatalf("want reload %s (%s), got %+v", model, reason, a)
	}
}

func wantNone(t *testing.T, a *Action) {
	t.Helper()
	if a != nil {
		t.Fatalf("want no action, got %+v", a)
	}
}

func TestYieldsAfterSustainedPressure(t *testing.T) {
	r := newRig()
	r.load("a", 6000)
	c := New(defaults(), nil)
	wantNone(t, step(t, c, r, 0))
	r.foreign = 9000 // a game: free = 16384-800-9000-6000 = 584 MB
	wantNone(t, step(t, c, r, 1*time.Second))
	wantNone(t, step(t, c, r, 3*time.Second))
	a := step(t, c, r, 4*time.Second) // 3 s after the signal started
	wantYield(t, a, "a", ReasonVRAMPressure)
	if a.FreeMB != 584 {
		t.Fatalf("free %d", a.FreeMB)
	}
}

func TestShortSpikeDoesNotYield(t *testing.T) {
	r := newRig()
	r.load("a", 6000)
	c := New(defaults(), nil)
	step(t, c, r, 0)
	r.foreign = 9000
	wantNone(t, step(t, c, r, 1*time.Second))
	wantNone(t, step(t, c, r, 3*time.Second))
	r.foreign = 0 // gone before sustain_seconds
	wantNone(t, step(t, c, r, 5*time.Second))
	r.foreign = 9000 // starts again: the timer starts from zero
	wantNone(t, step(t, c, r, 7*time.Second))
	wantNone(t, step(t, c, r, 9*time.Second))
	wantYield(t, step(t, c, r, 10*time.Second), "a", ReasonVRAMPressure)
}

func TestOwnModelFillingTheCardIsNotPressure(t *testing.T) {
	r := newRig()
	c := New(defaults(), nil)
	step(t, c, r, 0)
	r.load("big", 15000) // free = 16384-800-15000 = 584 MB, caused by us
	for s := 1; s <= 20; s++ {
		wantNone(t, step(t, c, r, time.Duration(s)*time.Second))
	}
	r.foreign = 300 // another program grows by >= 256 MB: now it is pressure
	step(t, c, r, 21*time.Second)
	step(t, c, r, 23*time.Second)
	wantYield(t, step(t, c, r, 24*time.Second), "big", ReasonVRAMPressure)
}

func TestYieldOrderAndHysteresis(t *testing.T) {
	r := newRig()
	r.total = 24576
	r.loaded = []Model{
		{Name: "res-high", VRAMMB: 5000, LastUsed: t0.Add(-1 * time.Hour)},
		{Name: "res-low-new", VRAMMB: 4000, LastUsed: t0.Add(-1 * time.Minute)},
		{Name: "res-low-old", VRAMMB: 4000, LastUsed: t0.Add(-2 * time.Hour)},
		{Name: "manual", VRAMMB: 1000, LastUsed: t0},
	}
	c := New(defaults(), []protocol.Resident{
		resident("res-high", 90, true), resident("res-low-new", 10, true), resident("res-low-old", 10, true),
	})
	step(t, c, r, 0)
	// used = 800 + 14000 ours = 14800; a game takes 9000 more: free = 24576-23800 = 776 MB.
	r.foreign = 9000
	step(t, c, r, 1*time.Second)
	a := step(t, c, r, 4*time.Second)
	wantYield(t, a, "manual", ReasonVRAMPressure) // non-resident first
	apply(t, c, r, a, 4*time.Second)
	// free = 1776: above 1024 but below the 2048 target -> keep going, by priority then LRU.
	a = step(t, c, r, 6*time.Second)
	wantYield(t, a, "res-low-old", ReasonVRAMPressure)
	apply(t, c, r, a, 6*time.Second)
	// free = 5776 >= target: stop.
	wantNone(t, step(t, c, r, 8*time.Second))
	wantNone(t, step(t, c, r, 10*time.Second))
	if !c.Active(t0.Add(10 * time.Second)) {
		t.Fatal("res-low-old waits for its reload: state yielded")
	}
}

func TestDemotionSignal(t *testing.T) {
	r := newRig()
	r.load("a", 6000)
	c := New(defaults(), []protocol.Resident{resident("a", 50, true)})
	step(t, c, r, 0)
	r.demotedMB = 1200 // dedicated -1200, shared +1200, plenty free
	step(t, c, r, 1*time.Second)
	wantNone(t, step(t, c, r, 3*time.Second))
	wantYield(t, step(t, c, r, 4*time.Second), "a", ReasonVRAMPressure)

	// Without per-process shared counters the signal is not available.
	r2 := newRig()
	r2.load("a", 6000)
	r2.sharedMeasured = false
	c2 := New(defaults(), nil)
	step(t, c2, r2, 0)
	r2.demotedMB = 1200
	for s := 1; s < 10; s++ {
		wantNone(t, step(t, c2, r2, time.Duration(s)*time.Second))
	}
}

// yieldA brings the rig into "game running, a yielded" at 4 s.
func yieldA(t *testing.T, c *Controller, r *rig) {
	t.Helper()
	r.load("a", 5000)
	step(t, c, r, 0)
	r.foreign = 10000
	step(t, c, r, 1*time.Second)
	a := step(t, c, r, 4*time.Second)
	wantYield(t, a, "a", ReasonVRAMPressure)
	apply(t, c, r, a, 4*time.Second)
}

func TestReloadWhenFreeAgain(t *testing.T) {
	r := newRig()
	c := New(defaults(), []protocol.Resident{resident("a", 50, true)})
	yieldA(t, c, r)
	if !c.Active(t0.Add(5 * time.Second)) {
		t.Fatal("state should be yielded")
	}
	r.foreign = 0 // game quits at 10 s
	wantNone(t, step(t, c, r, 10*time.Second))
	// min_off (120 s since the yield at 4 s) is not over before 124 s: the timer starts then.
	wantNone(t, step(t, c, r, 123*time.Second))
	wantNone(t, step(t, c, r, 124*time.Second))
	wantNone(t, step(t, c, r, 183*time.Second))
	a := step(t, c, r, 184*time.Second) // 60 s of good conditions
	wantReload(t, a, "a", ReasonPressureCleared)
	apply(t, c, r, a, 184*time.Second)
	if c.Active(t0.Add(185 * time.Second)) {
		t.Fatal("state should be available after the reload")
	}
}

func TestReloadConditionsResetTheTimer(t *testing.T) {
	r := newRig()
	c := New(defaults(), []protocol.Resident{resident("a", 50, true)})
	yieldA(t, c, r)
	r.foreign = 0
	step(t, c, r, 130*time.Second) // timer starts (min_off over)
	r.util, r.utilKnown = 80, true // another program busy on the GPU
	wantNone(t, step(t, c, r, 160*time.Second))
	r.util = 10
	step(t, c, r, 170*time.Second) // timer restarts
	wantNone(t, step(t, c, r, 220*time.Second))
	r.rule = true // a watcher rule matches: veto
	wantNone(t, step(t, c, r, 230*time.Second))
	wantNone(t, step(t, c, r, 400*time.Second))
	r.rule = false
	step(t, c, r, 401*time.Second)
	wantReload(t, step(t, c, r, 461*time.Second), "a", ReasonPressureCleared)
}

func TestReloadWhileRuleMatchesAllowed(t *testing.T) {
	r := newRig()
	cfg := defaults()
	cfg.ReloadWhileRuleMatches = true
	c := New(cfg, []protocol.Resident{resident("a", 50, true)})
	yieldA(t, c, r)
	r.foreign, r.rule = 0, true
	step(t, c, r, 130*time.Second)
	wantReload(t, step(t, c, r, 190*time.Second), "a", ReasonPressureCleared)
}

func TestReloadNeedsHeadroom(t *testing.T) {
	r := newRig()
	c := New(defaults(), []protocol.Resident{resident("a", 50, true)})
	yieldA(t, c, r) // last measured VRAM of a: 5000
	// free must be >= 5000 + 2048 = 7048; leave 7000 free.
	r.foreign = 16384 - 800 - 7000
	for s := 130; s <= 400; s += 30 {
		wantNone(t, step(t, c, r, time.Duration(s)*time.Second))
	}
	r.foreign -= 100 // 7100 free
	step(t, c, r, 401*time.Second)
	wantReload(t, step(t, c, r, 461*time.Second), "a", ReasonPressureCleared)
}

func TestResidentLoadsOnStartUsingFileSize(t *testing.T) {
	r := newRig()
	r.onDisk = map[string]int64{"b": 12000}
	c := New(defaults(), []protocol.Resident{resident("b", 50, true), resident("gone", 99, true)})
	// never loaded: needs its file size + headroom = 14048 MB, only 15584 free: ok.
	step(t, c, r, 0)
	a := step(t, c, r, 60*time.Second)
	wantReload(t, a, "b", ReasonResident) // "gone" is not on disk
	r.onDisk["b"] = 15000
	c2 := New(defaults(), []protocol.Resident{resident("b", 50, true)})
	for s := 0; s <= 300; s += 30 {
		wantNone(t, step(t, c2, r, time.Duration(s)*time.Second))
	}
}

func TestHighestPriorityReloadsFirstOneAtATime(t *testing.T) {
	r := newRig()
	c := New(defaults(), []protocol.Resident{resident("c", 10, true), resident("a", 80, true), resident("b", 50, true)})
	step(t, c, r, 0)
	a := step(t, c, r, 60*time.Second)
	wantReload(t, a, "a", ReasonResident)
	apply(t, c, r, a, 60*time.Second)
	wantNone(t, step(t, c, r, 61*time.Second)) // the timer restarts after each load
	wantNone(t, step(t, c, r, 120*time.Second))
	wantReload(t, step(t, c, r, 121*time.Second), "b", ReasonResident)
}

func TestFlapGuardPausesReloads(t *testing.T) {
	r := newRig()
	cfg := defaults()
	cfg.MinOff, cfg.ReloadAfter = 0, 0
	c := New(cfg, []protocol.Resident{resident("a", 50, true)})
	at := time.Duration(0)
	r.load("a", 5000)
	step(t, c, r, at)
	for i := 0; i < 3; i++ {
		r.foreign = 10000
		step(t, c, r, at+1*time.Second)
		a := step(t, c, r, at+4*time.Second)
		wantYield(t, a, "a", ReasonVRAMPressure)
		r.unload("a")
		until, paused := c.Yielded("a", t0.Add(at+4*time.Second))
		if paused != (i == 2) {
			t.Fatalf("yield %d: paused=%v", i+1, paused)
		}
		if paused && !until.Equal(t0.Add(at+4*time.Second+FlapPause)) {
			t.Fatalf("until %v", until)
		}
		r.foreign = 0
		if i < 2 {
			a = step(t, c, r, at+5*time.Second)
			wantReload(t, a, "a", ReasonPressureCleared)
			apply(t, c, r, a, at+5*time.Second)
		}
		at += 5 * time.Minute
	}
	// Paused: no reload, and the node is not "yielded" because of a paused model.
	wantNone(t, step(t, c, r, at))
	if c.Active(t0.Add(at)) {
		t.Fatal("a paused model does not keep the node yielded")
	}
	// A manual load lifts the pause.
	c.ManualLoad("a")
	wantReload(t, step(t, c, r, at+time.Second), "a", ReasonResident)
}

func TestFlapGuardExpires(t *testing.T) {
	c := New(defaults(), []protocol.Resident{resident("a", 50, true)})
	c.Yielded("a", t0)
	c.Yielded("a", t0.Add(20*time.Minute))
	// The first yield is older than 30 minutes now: no pause.
	if _, paused := c.Yielded("a", t0.Add(31*time.Minute)); paused {
		t.Fatal("yields outside the window must not count")
	}
}

func TestNonResidentYieldEndsWithTheEpisode(t *testing.T) {
	r := newRig()
	c := New(defaults(), nil)
	r.load("manual", 6000)
	step(t, c, r, 0)
	r.foreign = 9000
	step(t, c, r, 1*time.Second)
	a := step(t, c, r, 4*time.Second)
	wantYield(t, a, "manual", ReasonVRAMPressure)
	apply(t, c, r, a, 4*time.Second)
	// free = 6584 >= target: episode over, nothing waits -> available.
	wantNone(t, step(t, c, r, 6*time.Second))
	if c.Active(t0.Add(6 * time.Second)) {
		t.Fatal("nothing to reload: not yielded")
	}
	wantNone(t, step(t, c, r, 600*time.Second)) // never reloaded
}

func TestNothingLoadedIsNotYielded(t *testing.T) {
	r := newRig()
	c := New(defaults(), nil)
	step(t, c, r, 0)
	r.foreign = 15000
	for s := 1; s < 10; s++ {
		wantNone(t, step(t, c, r, time.Duration(s)*time.Second))
	}
	if c.Active(t0.Add(10 * time.Second)) {
		t.Fatal("nothing was yielded")
	}
}

func TestSelfActivitySuppressesDecisions(t *testing.T) {
	r := newRig()
	r.load("a", 6000)
	c := New(defaults(), []protocol.Resident{resident("b", 50, true)})
	step(t, c, r, 0)
	r.self = true // e.g. a benchmark allocates VRAM
	r.foreign = 9000
	for s := 1; s < 20; s++ {
		wantNone(t, step(t, c, r, time.Duration(s)*time.Second))
	}
	r.self, r.foreign = false, 0
	step(t, c, r, 20*time.Second)
	wantNone(t, step(t, c, r, 79*time.Second))
	wantReload(t, step(t, c, r, 80*time.Second), "b", ReasonResident)
}

func TestDisabledPressureStillKeepsResidents(t *testing.T) {
	r := newRig()
	r.load("a", 6000)
	cfg := defaults()
	cfg.Enabled = false
	c := New(cfg, []protocol.Resident{resident("b", 50, true)})
	step(t, c, r, 0)
	r.foreign = 9000
	for s := 1; s < 10; s++ {
		wantNone(t, step(t, c, r, time.Duration(s)*time.Second))
	}
	r.foreign = 0
	step(t, c, r, 10*time.Second)
	wantReload(t, step(t, c, r, 70*time.Second), "b", ReasonResident)
}

func TestRAMPressureYieldsCPUModelsOnly(t *testing.T) {
	r := newRig()
	r.noGPU = true
	r.ramTotal, r.ramUsed = 32768, 20000
	r.loaded = []Model{{Name: "cpu-model", RAMMB: 3000, CPU: true}}
	cfg := defaults()
	cfg.Enabled, cfg.RAMEnabled = false, true
	c := New(cfg, nil)
	step(t, c, r, 0)
	r.ramUsed = 31500 // the user opens something big: free 1268 < 2048
	step(t, c, r, 1*time.Second)
	a := step(t, c, r, 4*time.Second)
	wantYield(t, a, "cpu-model", ReasonRAMPressure)

	// RAM pressure never touches GPU models.
	r2 := newRig()
	r2.ramTotal, r2.ramUsed = 32768, 20000
	r2.load("gpu-model", 6000)
	c2 := New(cfg, nil)
	step(t, c2, r2, 0)
	r2.ramUsed = 31500
	for s := 1; s < 10; s++ {
		wantNone(t, step(t, c2, r2, time.Duration(s)*time.Second))
	}
}

func TestResidentsOnCPUNodeCheckRAM(t *testing.T) {
	r := newRig()
	r.noGPU = true
	r.onDisk = map[string]int64{"a": 2000}
	r.ramTotal, r.ramUsed = 16384, 13500 // 2884 free < 2000 + 1024
	c := New(defaults(), []protocol.Resident{resident("a", 50, true)})
	for s := 0; s <= 120; s += 30 {
		wantNone(t, step(t, c, r, time.Duration(s)*time.Second))
	}
	r.ramUsed = 13000 // 3384 free
	step(t, c, r, 121*time.Second)
	wantReload(t, step(t, c, r, 181*time.Second), "a", ReasonResident)
}

func TestReloadFailureRetriesLater(t *testing.T) {
	r := newRig()
	c := New(defaults(), []protocol.Resident{resident("a", 50, true)})
	step(t, c, r, 0)
	wantReload(t, step(t, c, r, 60*time.Second), "a", ReasonResident)
	c.ReloadFailed("a", t0.Add(60*time.Second))
	wantNone(t, step(t, c, r, 61*time.Second))
	wantNone(t, step(t, c, r, 60*time.Second+RetryAfterFailure-time.Second))
	step(t, c, r, 60*time.Second+RetryAfterFailure)
	wantReload(t, step(t, c, r, 120*time.Second+RetryAfterFailure), "a", ReasonResident)
}

func TestManualUnloadRestartsTheTimer(t *testing.T) {
	r := newRig()
	c := New(defaults(), []protocol.Resident{resident("a", 50, true)})
	step(t, c, r, 0)
	wantNone(t, step(t, c, r, 50*time.Second))
	c.ManualUnload("b")
	wantNone(t, step(t, c, r, 60*time.Second)) // would have reloaded without the reset
	wantReload(t, step(t, c, r, 120*time.Second), "a", ReasonResident)
}

func TestGameUnloadStartsMinOff(t *testing.T) {
	r := newRig()
	c := New(defaults(), []protocol.Resident{resident("a", 50, true)})
	c.GameUnloaded(t0)
	for s := 0; s < 120; s += 20 {
		wantNone(t, step(t, c, r, time.Duration(s)*time.Second))
	}
	step(t, c, r, 120*time.Second)
	wantReload(t, step(t, c, r, 180*time.Second), "a", ReasonResident)
}

func TestConfigChangePrunesPendingReloads(t *testing.T) {
	r := newRig()
	c := New(defaults(), []protocol.Resident{resident("a", 50, true)})
	yieldA(t, c, r)
	r.foreign = 0
	step(t, c, r, 6*time.Second)
	if !c.Active(t0.Add(6 * time.Second)) {
		t.Fatal("a waits for its reload")
	}
	// The user unloaded / disabled it in the GUI: the hub clears auto_reload.
	c.SetConfig(defaults(), []protocol.Resident{resident("a", 50, false)})
	if c.Active(t0.Add(7 * time.Second)) {
		t.Fatal("nothing waits any more")
	}
	for s := 130; s < 400; s += 30 {
		wantNone(t, step(t, c, r, time.Duration(s)*time.Second))
	}
}

func TestRunnerDownBlocksReload(t *testing.T) {
	r := newRig()
	r.down = true
	c := New(defaults(), []protocol.Resident{resident("a", 50, true)})
	for s := 0; s <= 300; s += 30 {
		wantNone(t, step(t, c, r, time.Duration(s)*time.Second))
	}
}

func TestEffectivePressureDefaults(t *testing.T) {
	var none protocol.NodeConfig
	if p := none.EffectivePressure(); p.Enabled || p.RAMEnabled || p.ReloadAfter != 60*time.Second {
		t.Fatalf("without a pressure object: %+v", p)
	}
	on := protocol.NodeConfig{Pressure: &protocol.PressureConfig{SustainSeconds: ptr(5)}}
	p := on.EffectivePressure()
	if !p.Enabled || p.Sustain != 5*time.Second || p.Poll != 2*time.Second || p.UnloadBelowFreeMB != 1024 ||
		p.MarginMB != 1024 || p.ReloadHeadroomMB != 2048 || p.ReloadMaxForeignUtil != 30 || p.MinOff != 120*time.Second ||
		p.ReloadWhileRuleMatches || p.RAMEnabled || p.RAMUnloadBelowFreeMB != 2048 {
		t.Fatalf("defaults: %+v", p)
	}
}

func TestGameRightAfterReloadStillCounts(t *testing.T) {
	r := newRig()
	cfg := defaults()
	cfg.MinOff, cfg.ReloadAfter = 0, 0
	c := New(cfg, []protocol.Resident{resident("a", 50, true)})
	step(t, c, r, 0)
	a := step(t, c, r, time.Second)
	wantReload(t, a, "a", ReasonResident)
	apply(t, c, r, a, time.Second)
	// The game starts before the next poll: the first sample after the reload
	// already contains it, but our usage is measured, so it is still growth.
	r.foreign = 10000
	step(t, c, r, 2*time.Second)
	step(t, c, r, 4*time.Second)
	wantYield(t, step(t, c, r, 5*time.Second), "a", ReasonVRAMPressure)
}

func TestEstimatedOwnUsageRestartsTheBaseline(t *testing.T) {
	r := newRig()
	c := New(defaults(), nil)
	s := r.sample(t0)
	c.Step(s)
	// Our model appears with an estimate that misses 400 MB of process overhead:
	// the "foreign" part rises by 400 MB, which must not count as growth.
	s = Sample{Now: t0.Add(time.Second), Loaded: []Model{{Name: "big", VRAMMB: 14800}}, OnDisk: r.onDisk,
		GPUs: []GPU{{TotalMB: 16384, UsedMB: 800 + 14800 + 400, OwnMB: 14800}}}
	for i := 1; i < 10; i++ {
		s.Now = t0.Add(time.Duration(i) * time.Second)
		wantNone(t, c.Step(s))
	}
}

// lmStudioRig reproduces test 1 on node-amd: our 7B model (4836 MB dedicated)
// is loaded, then LM Studio loads a 20B model; WDDM gives it 4844 MB dedicated
// plus 7743 MB shared instead of evicting our model. 2.9 GB stay free, so
// neither the free-VRAM nor the demotion signal fires.
func lmStudioRig() *rig {
	r := newRig()
	r.total, r.desktop = 16304, 3720
	r.foreignSharedKnown = true
	r.onDisk["a"] = 4700
	r.load("a", 4836)
	return r
}

func TestUnmetDemandYields(t *testing.T) {
	r := lmStudioRig()
	c := New(defaults(), []protocol.Resident{resident("a", 50, true)})
	if a := step(t, c, r, 0); a != nil {
		t.Fatalf("%+v", a)
	}
	r.foreign, r.foreignShared = 4844, 7743
	if free := r.sample(t0).GPUs[0].FreeMB(); free <= 1024 {
		t.Fatalf("rig: free %d must stay above unload_below_free_mb", free)
	}
	if a := step(t, c, r, time.Second); a != nil {
		t.Fatalf("yield before sustain: %+v", a)
	}
	a := step(t, c, r, 4*time.Second)
	wantYield(t, a, "a", ReasonVRAMPressure)
	if a.Signal != SignalUnmetDemand {
		t.Fatalf("signal %q", a.Signal)
	}
	apply(t, c, r, a, 4*time.Second)
	if !c.Active(t0.Add(5 * time.Second)) {
		t.Fatal("node must report yielded")
	}
	// LM Studio keeps its model in shared memory: our model must not come back,
	// although 7.7 GB of VRAM are free now.
	for at := 5 * time.Second; at < 10*time.Minute; at += 2 * time.Second {
		if a := step(t, c, r, at); a != nil {
			t.Fatalf("at %v: %+v", at, a)
		}
	}
	// LM Studio unloads: reload after reload_after_seconds.
	r.foreign, r.foreignShared = 0, 0
	var got *Action
	for at := 10 * time.Minute; at < 12*time.Minute && got == nil; at += 2 * time.Second {
		got = step(t, c, r, at)
	}
	wantReload(t, got, "a", ReasonPressureCleared)
}

func TestUnmetDemandNeedsTriggerAndShortage(t *testing.T) {
	cases := []struct {
		name          string
		trigger       int64
		known         bool
		foreign, shrd int64
	}{
		{"signal off", 0, true, 4844, 7743},
		{"not measured", 1024, false, 4844, 7743},
		{"small shared use", 1024, true, 4844, 900},
		{"enough free VRAM for it", 1024, true, 0, 2000}, // free 7748 >= 2000 + margin
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := lmStudioRig()
			cfg := defaults()
			cfg.ForeignSharedTriggerMB = tc.trigger
			c := New(cfg, nil)
			step(t, c, r, 0)
			r.foreign, r.foreignShared, r.foreignSharedKnown = tc.foreign, tc.shrd, tc.known
			for at := time.Second; at < 20*time.Second; at += time.Second {
				if a := step(t, c, r, at); a != nil {
					t.Fatalf("at %v: %+v", at, a)
				}
			}
		})
	}
}

// Memory another program waits for in shared memory is not free for a reload.
func TestReloadLeavesRoomForUnmetDemand(t *testing.T) {
	r := newRig()
	r.foreignSharedKnown = true
	cfg := defaults()
	cfg.ReloadAfter, cfg.MinOff = 0, 0
	c := New(cfg, []protocol.Resident{resident("a", 50, true)})
	// free 15584 - demand 9000 = 6584 < a (5000) + headroom 2048; no pressure
	// because 15584 >= 9000 + margin.
	r.foreignShared = 9000
	for at := time.Duration(0); at < time.Minute; at += 2 * time.Second {
		if a := step(t, c, r, at); a != nil {
			t.Fatalf("at %v: %+v", at, a)
		}
	}
	r.foreignShared = 1500 // 15584 - 1500 >= 7048
	var got *Action
	for at := time.Minute; at < 2*time.Minute && got == nil; at += 2 * time.Second {
		got = step(t, c, r, at)
	}
	wantReload(t, got, "a", ReasonResident)
}

func TestGameEvictedNonResidentComesBack(t *testing.T) {
	r := newRig()
	c := New(defaults(), nil) // "a" is no resident: the hub loaded it on demand
	c.GameUnloaded(t0, "a")
	if !c.HasEvicted() {
		t.Fatal("a waits to come back")
	}
	for s := 0; s < 120; s += 20 {
		wantNone(t, step(t, c, r, time.Duration(s)*time.Second))
	}
	step(t, c, r, 120*time.Second)
	a := step(t, c, r, 180*time.Second)
	wantReload(t, a, "a", ReasonResident)
	apply(t, c, r, a, 180*time.Second)
	if c.HasEvicted() {
		t.Fatal("a is back")
	}
}

func TestGameEvictedResidentIsNotDuplicated(t *testing.T) {
	c := New(defaults(), []protocol.Resident{resident("a", 50, true)})
	c.GameUnloaded(t0, "a")
	if c.HasEvicted() {
		t.Fatal("a resident comes back through its own entry")
	}
}

func TestManualUnloadForgetsEvicted(t *testing.T) {
	c := New(defaults(), nil)
	c.GameUnloaded(t0, "a")
	c.ManualUnload("a")
	if c.HasEvicted() {
		t.Fatal("the user does not want a back")
	}
}
