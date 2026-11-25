// Package yield decides when the node steps back for the person using the machine (E2-05 B,
// ARCHITECTURE D24): it freezes the model processes on keyboard/mouse input, on foreign CPU load
// or on battery power, continues them when the signals are gone, and unloads the models when a
// pause lasts too long.
//
// It is pure logic like package pressure: the agent feeds a Sample per poll and executes the
// Action Step returns, so every rule can be unit-tested with a synthetic clock.
package yield

import (
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/activity"
)

// Sample is what the agent measured at one poll.
type Sample struct {
	Now      time.Time
	Activity activity.Sample
	// IgnoreCPU: the node itself is loading a model or benchmarking, so the CPU it uses would
	// look like foreign load. The CPU signal stands still meanwhile.
	IgnoreCPU bool
	// Models is the number of loaded models (an unload only makes sense with some).
	Models int
}

// Kind is the type of an Action.
type Kind int

// Action kinds.
const (
	// Freeze: suspend the model processes, take no new work (state paused).
	Freeze Kind = iota + 1
	// Thaw: continue the model processes.
	Thaw
	// Unload: the pause lasted too long; unload the models (the processes stay frozen until then).
	Unload
)

// Action is what the agent should do.
type Action struct {
	Kind Kind
	// Reasons that hold now (Freeze), or held when the pause ended (Thaw).
	Reasons []string
	// PausedFor is how long the pause lasted (Thaw, Unload).
	PausedFor time.Duration
}

// Controller holds the state between polls.
type Controller struct {
	cfg protocol.Yield

	paused   bool
	since    time.Time
	reasons  []string
	unloaded bool

	// CPU load with hysteresis: it becomes a reason after cfg.Sustain above the threshold and
	// stops being one after cfg.Resume below it.
	cpuActive bool
	cpuSince  time.Time // start of the current run above (or, while active, below) the threshold
	cpuRun    bool      // cpuSince is set
}

// New creates a controller.
func New(cfg protocol.Yield) *Controller { return &Controller{cfg: cfg} }

// SetConfig replaces the thresholds; the state is kept.
func (c *Controller) SetConfig(cfg protocol.Yield) { c.cfg = cfg }

// Paused reports whether the node is frozen for local use.
func (c *Controller) Paused() bool { return c.paused }

// Reasons are the signals that hold the pause (empty when running).
func (c *Controller) Reasons() []string { return append([]string(nil), c.reasons...) }

// Since is when the current pause began (zero when running).
func (c *Controller) Since() time.Time { return c.since }

// Step takes one sample and returns at most one action.
func (c *Controller) Step(s Sample) *Action {
	if !c.cfg.Enabled {
		if c.paused {
			return c.thaw(s.Now)
		}
		c.cpuActive, c.cpuRun = false, false
		return nil
	}
	reasons := c.reasonsFor(s)
	switch {
	case !c.paused && len(reasons) > 0:
		c.paused, c.since, c.reasons, c.unloaded = true, s.Now, reasons, false
		return &Action{Kind: Freeze, Reasons: reasons}
	case c.paused && len(reasons) == 0:
		return c.thaw(s.Now)
	case c.paused:
		c.reasons = reasons
		after := c.cfg.UnloadAfterPause
		if after > 0 && !c.unloaded && s.Models > 0 && s.Now.Sub(c.since) >= after {
			c.unloaded = true
			return &Action{Kind: Unload, Reasons: reasons, PausedFor: s.Now.Sub(c.since)}
		}
	}
	return nil
}

func (c *Controller) thaw(now time.Time) *Action {
	a := &Action{Kind: Thaw, Reasons: c.reasons, PausedFor: now.Sub(c.since)}
	c.paused, c.since, c.reasons, c.unloaded = false, time.Time{}, nil, false
	c.cpuActive, c.cpuRun = false, false
	return a
}

// reasonsFor evaluates the three signals; a signal that cannot be measured never counts.
func (c *Controller) reasonsFor(s Sample) []string {
	var r []string
	a := s.Activity
	if a.InputKnown && a.InputIdle < c.cfg.Idle {
		r = append(r, protocol.PauseInput)
	}
	if c.cpu(s) {
		r = append(r, protocol.PauseCPULoad)
	}
	if c.cfg.PauseOnBattery && a.BatteryKnown && a.OnBattery {
		r = append(r, protocol.PauseBattery)
	}
	return r
}

// cpu applies the sustain / resume hysteresis to the foreign CPU load.
func (c *Controller) cpu(s Sample) bool {
	if c.cfg.CPUPercent <= 0 {
		c.cpuActive, c.cpuRun = false, false
		return false
	}
	if s.IgnoreCPU || !s.Activity.CPUKnown {
		return c.cpuActive // no new information: the state stands, its timer does not run
	}
	above := s.Activity.ForeignCPU > c.cfg.CPUPercent
	if above == c.cpuActive { // the load agrees with the current state: nothing is changing
		c.cpuRun = false
		return c.cpuActive
	}
	if !c.cpuRun {
		c.cpuSince, c.cpuRun = s.Now, true
	}
	need := c.cfg.Sustain
	if c.cpuActive {
		need = c.cfg.Resume
	}
	if s.Now.Sub(c.cpuSince) >= need {
		c.cpuActive, c.cpuRun = !c.cpuActive, false
	}
	return c.cpuActive
}
