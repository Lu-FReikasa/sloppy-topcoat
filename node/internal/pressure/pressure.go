// Package pressure decides when the node gives memory back to other programs
// and when it brings its resident models back (E1-06, docs/PROTOCOL.md "VRAM
// pressure and resident models").
//
// It is pure logic: the agent feeds a Sample per poll and executes the action
// Step returns (at most one per poll, so memory counters can settle between
// two unloads), then reports the outcome back. Every rule can therefore be
// unit-tested with a synthetic clock.
package pressure

import (
	"sort"
	"strings"
	"sync"
	"time"

	"topcoat/node/internal/protocol"
)

// Fixed thresholds (documented in docs/PROTOCOL.md).
const (
	// ForeignGrowthMB: low free VRAM only counts as pressure if other programs'
	// usage grew at least this much since the node's models last changed, so a
	// model that fills the card by itself is not yielded right after loading.
	ForeignGrowthMB = 256
	// DemotionDropMB / DemotionRiseMB: our processes' dedicated VRAM fell this
	// far below its peak while their shared memory rose this much (the driver
	// pages our buffers out, typical for Windows/WDDM when a game needs VRAM).
	DemotionDropMB = 256
	DemotionRiseMB = 128
	// Flap guard: FlapLimit yields of one model within FlapWindow pause its
	// automatic reload for FlapPause.
	FlapWindow = 30 * time.Minute
	FlapLimit  = 3
	FlapPause  = 60 * time.Minute
	// RetryAfterFailure delays the next automatic load of a model whose load failed.
	RetryAfterFailure = 5 * time.Minute
	// EvictedKeep: how long a model a game unloaded stays a reload candidate.
	EvictedKeep = 12 * time.Hour
)

// Signals that start a VRAM yield, carried as "signal" in model_unloaded.
const (
	SignalLowFree     = "low_free"
	SignalDemotion    = "demotion"
	SignalUnmetDemand = "unmet_demand"
)

// Reasons carried in model_unloaded / model_loaded events.
const (
	ReasonVRAMPressure    = "vram_pressure"
	ReasonRAMPressure     = "ram_pressure"
	ReasonPressureCleared = "pressure_cleared"
	ReasonResident        = "resident"
)

// GPU is one discrete GPU in a sample.
type GPU struct {
	TotalMB int64
	// UsedMB is dedicated VRAM used by all processes.
	UsedMB int64
	// OwnMB is the dedicated VRAM of the node's model processes: measured when
	// OwnMeasured, otherwise estimated from the loaded models.
	OwnMB       int64
	OwnMeasured bool
	// OwnSharedMB is the shared (system) memory of our processes; only
	// meaningful when SharedMeasured (needed for the demotion signal).
	OwnSharedMB    int64
	SharedMeasured bool
	// ForeignUtil is other programs' GPU utilization in percent (UtilKnown).
	ForeignUtil float64
	UtilKnown   bool
	// ForeignSharedMaxMB is the largest shared (system) memory use of a single
	// other process on this GPU (ForeignSharedKnown): a program the driver
	// placed in system memory because VRAM was full.
	ForeignSharedMaxMB int64
	ForeignSharedKnown bool
}

// FreeMB is total minus used, never negative.
func (g GPU) FreeMB() int64 { return max(g.TotalMB-g.UsedMB, 0) }

// ForeignMB is the VRAM used by other programs.
func (g GPU) ForeignMB() int64 { return max(g.UsedMB-g.OwnMB, 0) }

// Model is a loaded model.
type Model struct {
	Name   string
	VRAMMB int64
	RAMMB  int64
	// CPU is true for models running on the CPU backend (RAM pressure applies).
	CPU      bool
	LastUsed time.Time
}

// Sample is what the agent measured at one poll.
type Sample struct {
	Now time.Time
	// GPUs are the node's discrete GPUs (iGPUs are not watched). The first
	// one with the most VRAM is used for reload decisions.
	GPUs       []GPU
	RAMTotalMB int64
	RAMUsedMB  int64
	Loaded     []Model
	// OnDisk maps model names to their file size in MB; only models on disk
	// can be reloaded.
	OnDisk map[string]int64
	// RuleActive: a watcher rule matches or its cooldown runs.
	RuleActive bool
	// SelfActivity: the node is loading a model or benchmarking; memory
	// changes are its own, so no decisions are taken.
	SelfActivity bool
	// RunnerDown: the runner is unhealthy; nothing can be loaded.
	RunnerDown bool
}

// Kind is the type of an Action.
type Kind int

// Action kinds.
const (
	Yield Kind = iota + 1
	Reload
)

// Action is what the agent should do now.
type Action struct {
	Kind          Kind
	Model         string
	ContextLength int
	Reason        string
	// Signal is the VRAM signal behind a yield (SignalLowFree, ...).
	Signal string
	// FreeMB is the free memory (VRAM or RAM) when the decision was taken.
	FreeMB int64
}

type gpuBase struct {
	foreignMin   int64
	peakOwn      int64
	sharedAtPeak int64
}

// Controller holds the state between polls. It is safe for concurrent use.
type Controller struct {
	mu        sync.Mutex
	cfg       protocol.Pressure
	residents []protocol.Resident

	loadedKey   string
	gpuBase     []gpuBase
	ramForeign  int64
	baseSet     bool
	vramSince   time.Time
	ramSince    time.Time
	vramEpisode bool
	vramSignal  string
	ramEpisode  bool
	yielded     map[string]bool
	// evicted: models a game unloaded that are no resident (the hub had loaded them on
	// demand); they come back like a resident once the game is over (reload candidates).
	evicted   map[string]time.Time
	history   map[string][]time.Time
	paused    map[string]time.Time
	retry     map[string]time.Time
	lastVRAM  map[string]int64
	lastRAM   map[string]int64
	lastOff   time.Time
	condModel string
	condSince time.Time
}

// New returns a controller with the given configuration.
func New(cfg protocol.Pressure, residents []protocol.Resident) *Controller {
	c := &Controller{
		yielded:  map[string]bool{},
		evicted:  map[string]time.Time{},
		history:  map[string][]time.Time{},
		paused:   map[string]time.Time{},
		retry:    map[string]time.Time{},
		lastVRAM: map[string]int64{},
		lastRAM:  map[string]int64{},
	}
	c.SetConfig(cfg, residents)
	return c
}

// SetConfig replaces the configuration (called whenever the hub sends one).
func (c *Controller) SetConfig(cfg protocol.Pressure, residents []protocol.Resident) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.cfg = cfg
	c.residents = append([]protocol.Resident(nil), residents...)
	if !cfg.Enabled {
		c.vramEpisode, c.vramSince = false, time.Time{}
	}
	if !cfg.RAMEnabled {
		c.ramEpisode, c.ramSince = false, time.Time{}
	}
	for m := range c.yielded {
		if r, ok := c.resident(m); !ok || !r.Reloads() {
			delete(c.yielded, m)
		}
	}
}

func (c *Controller) resident(model string) (protocol.Resident, bool) {
	for _, r := range c.residents {
		if r.Model == model {
			return r, true
		}
	}
	return protocol.Resident{}, false
}

// priority orders yields: non-residents first (-1), then by resident priority.
func (c *Controller) priority(model string) int {
	if r, ok := c.resident(model); ok {
		return r.EffectivePriority()
	}
	return -1
}

func loadedKey(models []Model) string {
	names := make([]string, 0, len(models))
	for _, m := range models {
		names = append(names, m.Name)
	}
	sort.Strings(names)
	return strings.Join(names, "\x00")
}

// ramMeasured reports whether every loaded model has a measured RAM figure.
func ramMeasured(s Sample) bool {
	for _, m := range s.Loaded {
		if m.RAMMB <= 0 {
			return false
		}
	}
	return true
}

// ownRAM is the RAM of the node's model processes.
func ownRAM(s Sample) int64 {
	var sum int64
	for _, m := range s.Loaded {
		sum += m.RAMMB
	}
	return sum
}

// observe updates measurements and baselines from a sample.
func (c *Controller) observe(s Sample) {
	for _, m := range s.Loaded {
		if m.VRAMMB > 0 {
			c.lastVRAM[m.Name] = m.VRAMMB
		}
		if m.RAMMB > 0 {
			c.lastRAM[m.Name] = m.RAMMB
		}
		delete(c.yielded, m.Name) // back (loaded manually or reloaded)
	}
	key := loadedKey(s.Loaded)
	ramForeign := max(s.RAMUsedMB-ownRAM(s), 0)
	if !c.baseSet || len(c.gpuBase) != len(s.GPUs) {
		c.loadedKey, c.baseSet = key, true
		c.gpuBase = make([]gpuBase, len(s.GPUs))
		for i, g := range s.GPUs {
			c.gpuBase[i] = gpuBase{foreignMin: g.ForeignMB(), peakOwn: g.OwnMB, sharedAtPeak: g.OwnSharedMB}
		}
		c.ramForeign = ramForeign
		return
	}
	if key != c.loadedKey {
		// Our own usage changed legitimately: restart the demotion peak. The
		// foreign baseline stays when our usage is measured (a game starting
		// right after a reload must still count as growth); an estimate of our
		// usage is off by per-process overhead, so the baseline restarts then.
		c.loadedKey = key
		for i, g := range s.GPUs {
			b := &c.gpuBase[i]
			b.peakOwn, b.sharedAtPeak = g.OwnMB, g.OwnSharedMB
			if !g.OwnMeasured {
				b.foreignMin = g.ForeignMB()
			}
		}
		if !ramMeasured(s) {
			c.ramForeign = ramForeign
		}
	}
	for i, g := range s.GPUs {
		b := &c.gpuBase[i]
		b.foreignMin = min(b.foreignMin, g.ForeignMB())
		if g.OwnMB > b.peakOwn {
			b.peakOwn, b.sharedAtPeak = g.OwnMB, g.OwnSharedMB
		}
	}
	c.ramForeign = min(c.ramForeign, ramForeign)
}

// demand is the unmet VRAM demand of another program on g: its shared
// memory use when that reaches foreign_shared_trigger_mb, else 0.
func (c *Controller) demand(g GPU) int64 {
	t := c.cfg.ForeignSharedTriggerMB
	if t <= 0 || !g.ForeignSharedKnown || g.ForeignSharedMaxMB < t {
		return 0
	}
	return g.ForeignSharedMaxMB
}

// vramSignals reports whether any GPU shows pressure (and which signal) and
// whether any GPU is below the yield target, plus the lowest free VRAM.
func (c *Controller) vramSignals(s Sample) (pressure, belowTarget bool, minFree int64, signal string) {
	minFree = -1
	for i, g := range s.GPUs {
		free := g.FreeMB()
		if minFree < 0 || free < minFree {
			minFree = free
		}
		b := c.gpuBase[i]
		lowFree := free < c.cfg.UnloadBelowFreeMB && g.ForeignMB()-b.foreignMin >= ForeignGrowthMB
		demoted := g.OwnMeasured && g.SharedMeasured &&
			b.peakOwn-g.OwnMB >= DemotionDropMB && g.OwnSharedMB-b.sharedAtPeak >= DemotionRiseMB
		// Unmet demand: another program sits in system memory and the free
		// VRAM could not take it (WDDM places new allocations of a program
		// in shared memory instead of evicting ours).
		d := c.demand(g)
		unmet := d > 0 && free < d+c.cfg.MarginMB
		switch {
		case unmet && signal == "":
			signal = SignalUnmetDemand
		case demoted && signal == "":
			signal = SignalDemotion
		case lowFree && signal == "":
			signal = SignalLowFree
		}
		if lowFree || demoted || unmet {
			pressure = true
		}
		if free < c.cfg.UnloadBelowFreeMB+c.cfg.MarginMB || unmet {
			belowTarget = true
		}
	}
	return pressure, belowTarget, max(minFree, 0), signal
}

func (c *Controller) ramSignals(s Sample) (pressure, belowTarget bool, free int64) {
	if s.RAMTotalMB <= 0 {
		return false, false, 0
	}
	free = max(s.RAMTotalMB-s.RAMUsedMB, 0)
	foreign := max(s.RAMUsedMB-ownRAM(s), 0)
	pressure = free < c.cfg.RAMUnloadBelowFreeMB && foreign-c.ramForeign >= ForeignGrowthMB
	belowTarget = free < c.cfg.RAMUnloadBelowFreeMB+c.cfg.RAMMarginMB
	return pressure, belowTarget, free
}

// pickYield returns the next model to unload: GPU models for VRAM pressure,
// CPU models for RAM pressure; non-residents first, then lowest priority,
// then least recently used.
func (c *Controller) pickYield(s Sample, cpu bool) (Model, bool) {
	var cands []Model
	for _, m := range s.Loaded {
		if m.CPU == cpu {
			cands = append(cands, m)
		}
	}
	if len(cands) == 0 {
		return Model{}, false
	}
	sort.SliceStable(cands, func(i, j int) bool {
		pi, pj := c.priority(cands[i].Name), c.priority(cands[j].Name)
		if pi != pj {
			return pi < pj
		}
		if !cands[i].LastUsed.Equal(cands[j].LastUsed) {
			return cands[i].LastUsed.Before(cands[j].LastUsed)
		}
		return cands[i].Name < cands[j].Name
	})
	return cands[0], true
}

// Step evaluates a sample and returns the action to take, if any.
func (c *Controller) Step(s Sample) *Action {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.observe(s)
	if s.SelfActivity {
		c.vramSince, c.ramSince = time.Time{}, time.Time{}
		c.resetCond()
		return nil
	}

	vramP, vramBelow, minFree, signal := c.vramSignals(s)
	vramP = vramP && c.cfg.Enabled
	if vramP && signal != "" {
		c.vramSignal = signal
	}
	ramP, ramBelow, ramFree := c.ramSignals(s)
	ramP = ramP && c.cfg.RAMEnabled

	vramTrig := since(&c.vramSince, vramP, s.Now) >= c.cfg.Sustain && vramP
	ramTrig := since(&c.ramSince, ramP, s.Now) >= c.cfg.Sustain && ramP

	if a := c.yieldFor(s, vramTrig, vramBelow && c.cfg.Enabled, &c.vramEpisode, false, ReasonVRAMPressure, minFree); a != nil {
		a.Signal = c.vramSignal
		return a
	}
	if a := c.yieldFor(s, ramTrig, ramBelow && c.cfg.RAMEnabled, &c.ramEpisode, true, ReasonRAMPressure, ramFree); a != nil {
		return a
	}
	if vramP || ramP || c.vramEpisode || c.ramEpisode {
		c.resetCond() // no reloads while memory is short
		return nil
	}
	return c.reloadStep(s)
}

// since tracks when a signal started; it returns how long it has held (0 when off).
func since(start *time.Time, on bool, now time.Time) time.Duration {
	if !on {
		*start = time.Time{}
		return 0
	}
	if start.IsZero() {
		*start = now
	}
	return now.Sub(*start)
}

func (c *Controller) yieldFor(s Sample, trig, below bool, episode *bool, cpu bool, reason string, free int64) *Action {
	if !trig && (!*episode || !below) {
		if *episode && !below {
			*episode = false
		}
		return nil
	}
	m, ok := c.pickYield(s, cpu)
	if !ok {
		return nil
	}
	*episode = true
	c.resetCond()
	return &Action{Kind: Yield, Model: m.Name, Reason: reason, FreeMB: free}
}

func (c *Controller) resetCond() {
	c.condModel, c.condSince = "", time.Time{}
}

// reloadCandidates returns residents that should be loaded, best first.
func (c *Controller) reloadCandidates(s Sample) []protocol.Resident {
	loaded := map[string]bool{}
	for _, m := range s.Loaded {
		loaded[m.Name] = true
	}
	var out []protocol.Resident
	cands := append([]protocol.Resident(nil), c.residents...)
	for m, at := range c.evicted {
		if s.Now.Sub(at) > EvictedKeep {
			delete(c.evicted, m)
			continue
		}
		if _, ok := c.resident(m); !ok {
			low := 0 // non-residents come back after the residents
			cands = append(cands, protocol.Resident{Model: m, Priority: &low})
		}
	}
	for _, r := range cands {
		if !r.Reloads() || loaded[r.Model] {
			continue
		}
		if _, onDisk := s.OnDisk[r.Model]; !onDisk {
			continue
		}
		if until, ok := c.paused[r.Model]; ok && s.Now.Before(until) {
			continue
		}
		if until, ok := c.retry[r.Model]; ok && s.Now.Before(until) {
			continue
		}
		out = append(out, r)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].EffectivePriority() > out[j].EffectivePriority() })
	return out
}

// primary returns the discrete GPU used for reload decisions (most VRAM).
func primary(gpus []GPU) (GPU, bool) {
	if len(gpus) == 0 {
		return GPU{}, false
	}
	best := gpus[0]
	for _, g := range gpus[1:] {
		if g.TotalMB > best.TotalMB {
			best = g
		}
	}
	return best, true
}

func (c *Controller) canReload(s Sample, r protocol.Resident) bool {
	if s.RunnerDown || (s.RuleActive && !c.cfg.ReloadWhileRuleMatches) {
		return false
	}
	if !c.lastOff.IsZero() && s.Now.Sub(c.lastOff) < c.cfg.MinOff {
		return false
	}
	size := s.OnDisk[r.Model]
	if g, ok := primary(s.GPUs); ok {
		need := size
		if v, ok := c.lastVRAM[r.Model]; ok {
			need = v
		}
		// Memory another program waits for in system memory is not free for us.
		if g.FreeMB()-c.demand(g) < need+c.cfg.ReloadHeadroomMB {
			return false
		}
		return !g.UtilKnown || g.ForeignUtil <= c.cfg.ReloadMaxForeignUtil
	}
	if s.RAMTotalMB <= 0 {
		return true // nothing to check against
	}
	need := size
	if v, ok := c.lastRAM[r.Model]; ok {
		need = v
	}
	return s.RAMTotalMB-s.RAMUsedMB >= need+c.cfg.RAMReloadHeadroomMB
}

func (c *Controller) reloadStep(s Sample) *Action {
	cands := c.reloadCandidates(s)
	if len(cands) == 0 {
		c.resetCond()
		return nil
	}
	top := cands[0]
	if !c.canReload(s, top) {
		c.resetCond()
		return nil
	}
	if c.condModel != top.Model || c.condSince.IsZero() {
		c.condModel, c.condSince = top.Model, s.Now
	}
	if s.Now.Sub(c.condSince) < c.cfg.ReloadAfter {
		return nil
	}
	c.resetCond()
	reason := ReasonResident
	if c.yielded[top.Model] {
		reason = ReasonPressureCleared
	}
	free := int64(0)
	if g, ok := primary(s.GPUs); ok {
		free = g.FreeMB()
	} else if s.RAMTotalMB > 0 {
		free = max(s.RAMTotalMB-s.RAMUsedMB, 0)
	}
	return &Action{Kind: Reload, Model: top.Model, ContextLength: top.ContextLength, Reason: reason, FreeMB: free}
}

// Yielded records that model was unloaded because of pressure. If it was
// yielded FlapLimit times within FlapWindow its automatic reload is paused and
// the pause end is returned with paused=true.
func (c *Controller) Yielded(model string, now time.Time) (until time.Time, paused bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.lastOff = now
	if r, ok := c.resident(model); ok && r.Reloads() {
		c.yielded[model] = true
	}
	var recent []time.Time
	for _, t := range c.history[model] {
		if now.Sub(t) < FlapWindow {
			recent = append(recent, t)
		}
	}
	recent = append(recent, now)
	if len(recent) >= FlapLimit {
		delete(c.history, model)
		until = now.Add(FlapPause)
		c.paused[model] = until
		return until, true
	}
	c.history[model] = recent
	return time.Time{}, false
}

// Reloaded records a successful automatic load.
func (c *Controller) Reloaded(model string, vramMB, ramMB int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.yielded, model)
	delete(c.evicted, model)
	delete(c.retry, model)
	if vramMB > 0 {
		c.lastVRAM[model] = vramMB
	}
	if ramMB > 0 {
		c.lastRAM[model] = ramMB
	}
}

// ReloadFailed delays the next automatic load of model.
func (c *Controller) ReloadFailed(model string, now time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.retry[model] = now.Add(RetryAfterFailure)
	c.resetCond()
}

// ManualLoad lifts a flap-guard pause and retry delay (the user wants the model).
func (c *Controller) ManualLoad(model string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.paused, model)
	delete(c.retry, model)
	delete(c.yielded, model)
	delete(c.evicted, model)
}

// ManualUnload forgets a pending reload of model and restarts the reload timer.
func (c *Controller) ManualUnload(model string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.yielded, model)
	delete(c.evicted, model)
	c.resetCond()
}

// GameUnloaded records that a watcher rule unloaded everything (min-off time). The models it
// unloaded come back after the game like residents, also those nobody configured as resident.
func (c *Controller) GameUnloaded(now time.Time, models ...string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.lastOff = now
	for _, m := range models {
		if _, ok := c.resident(m); !ok {
			c.evicted[m] = now
		}
	}
	c.resetCond()
}

// Active reports whether the node is in state "yielded": an episode is running
// or a yielded model with auto_reload waits for its reload.
func (c *Controller) Active(now time.Time) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.vramEpisode || c.ramEpisode {
		return true
	}
	for m := range c.yielded {
		if until, ok := c.paused[m]; ok && now.Before(until) {
			continue
		}
		return true
	}
	return false
}

// HasEvicted reports whether a model a game unloaded still waits to come back.
func (c *Controller) HasEvicted() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.evicted) > 0
}
