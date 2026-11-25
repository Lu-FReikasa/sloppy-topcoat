package agent

import (
	"context"
	"os"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/activity"
	"topcoat/node/internal/providers/runner"
	"topcoat/node/internal/yield"
)

// Local use comes first (E2-05 B, ARCHITECTURE D24). yieldLoop looks every yield.poll_seconds at
// what the person at this machine does; yield.Controller decides, and this file executes:
//   - Freeze: the model processes are suspended where they are, the node state becomes
//     "paused", requests in flight get a proxy_status "paused" instead of a timeout, new ones are
//     refused (503 busy, so the hub asks another node), loads are refused.
//   - Thaw: the processes continue at the same place.
//   - Unload: a pause that lasts too long gives the memory back.

func (a *Agent) isPaused() bool { return a.paused.Load() }

func (a *Agent) yieldLoop(ctx context.Context) {
	for {
		t := time.NewTimer(a.yieldPoll())
		select {
		case <-ctx.Done():
			t.Stop()
			return
		case <-t.C:
		}
		a.yieldStep(ctx)
	}
}

// yieldPoll is the time to the next look: yield.poll_seconds, stretched while the node has
// nothing to do (energy profile, E2-05 E).
func (a *Agent) yieldPoll() time.Duration {
	if a.opts.YieldPoll > 0 {
		return a.opts.YieldPoll
	}
	cfg, _, _ := a.config()
	if cfg == nil {
		return protocol.DefaultYieldPollSeconds * time.Second
	}
	a.mu.Lock()
	dedicated := a.dedicatedGPU
	a.mu.Unlock()
	return cfg.EffectiveYieldFor(dedicated).Poll * time.Duration(a.idleBackoff(cfg))
}

// ownPIDs are the node's own processes: the agent and its model servers.
func ownPIDs(loaded []runner.LoadedModel) []int {
	pids := []int{os.Getpid()}
	for _, l := range loaded {
		if l.PID > 0 {
			pids = append(pids, l.PID)
		}
	}
	return pids
}

// yieldStep takes one sample and executes at most one action.
func (a *Agent) yieldStep(ctx context.Context) {
	cfg, _, r := a.config()
	if cfg == nil || r == nil {
		return
	}
	a.mu.Lock()
	dedicated := a.dedicatedGPU
	a.mu.Unlock()
	y := cfg.EffectiveYieldFor(dedicated)
	a.yc.SetConfig(y)
	s := yield.Sample{Now: a.opts.Now(), IgnoreCPU: a.selfActivity()}
	loaded, err := r.Loaded(ctx)
	if err == nil {
		s.Models = len(loaded)
	}
	if y.Enabled && a.opts.Activity != nil {
		s.Activity = a.opts.Activity.Sample(ctx, ownPIDs(loaded))
		a.noteInputErr(s.Activity)
		a.noteCPU(s.Activity)
	}
	act := a.yc.Step(s)
	if act == nil {
		return
	}
	switch act.Kind {
	case yield.Freeze:
		a.freeze(ctx, r, act.Reasons, len(loaded))
	case yield.Thaw:
		a.thaw(ctx, r, act)
	case yield.Unload:
		a.unloadAfterPause(ctx, r, loaded, act)
	}
}

// noteInputErr logs once why keyboard/mouse input cannot be seen.
func (a *Agent) noteInputErr(s activity.Sample) {
	if s.InputErr == "" || s.InputErr == a.inputErrSeen {
		return
	}
	a.inputErrSeen = s.InputErr
	a.log.Warn("the node cannot see keyboard/mouse input; it works on CPU load and battery only", "why", s.InputErr)
}

// noteCPU logs once (and again when it changes) which measures the CPU load is built from and
// which the machine did not give.
func (a *Agent) noteCPU(s activity.Sample) {
	if !s.CPUKnown || s.CPUNote == "" || s.CPUNote == a.cpuNoteSeen {
		return
	}
	a.cpuNoteSeen = s.CPUNote
	a.log.Info("CPU load of other programs is measured with", "measures", s.CPUNote)
}

func (a *Agent) freeze(ctx context.Context, r runner.ModelRunner, reasons []string, models int) {
	if fz, ok := r.(runner.Freezer); ok {
		if err := fz.Freeze(ctx); err != nil {
			a.log.Error("freeze models", "err", err)
		}
	}
	a.mu.Lock()
	a.pauseReasons, a.pausedAt = reasons, a.opts.Now()
	a.mu.Unlock()
	a.paused.Store(true)
	streams := a.notifyStreams(protocol.ProxyPaused, reasons)
	a.log.Info("paused for local use", "reasons", reasons, "models", models, "streams", streams)
	a.emit(protocol.EventPaused, map[string]any{"reasons": reasons, "models": models, "streams": streams})
	a.refreshState(ctx)
	a.sendHeartbeat(ctx)
}

func (a *Agent) thaw(ctx context.Context, r runner.ModelRunner, act *yield.Action) {
	if fz, ok := r.(runner.Freezer); ok {
		if err := fz.Thaw(ctx); err != nil {
			a.log.Error("continue models", "err", err)
		}
	}
	a.paused.Store(false)
	a.mu.Lock()
	a.pauseReasons = nil
	a.mu.Unlock()
	streams := a.notifyStreams(protocol.ProxyRunning, nil)
	a.log.Info("continuing after local use", "paused", act.PausedFor.Round(time.Second), "streams", streams)
	a.emit(protocol.EventResumed, map[string]any{"paused_seconds": int(act.PausedFor.Seconds()), "reasons": act.Reasons})
	a.refreshState(ctx)
	a.sendHeartbeat(ctx)
}

// unloadAfterPause gives the memory back after a pause that lasted unload_after_pause_seconds.
// Residents come back on their own once the pause is over (pressureStep reloads them then); the
// hub loads other models on demand. A half-finished chat answer ends here; the hub continues
// it with the Continue logic (status truncated).
func (a *Agent) unloadAfterPause(ctx context.Context, r runner.ModelRunner, loaded []runner.LoadedModel, act *yield.Action) {
	a.cancelLoads(ctx, "", "pause_timeout")
	for _, l := range loaded {
		if err := r.Unload(ctx, l.Name); err != nil {
			a.log.Error("unload after pause", "model", l.Name, "err", err)
			a.emit(protocol.EventError, map[string]any{"message": "unload " + l.Name + " after a pause failed: " + err.Error()})
			continue
		}
		a.forget(l.Name)
		data := unloadedData(l, "pause_timeout")
		data["paused_seconds"] = int(act.PausedFor.Seconds())
		a.emit(protocol.EventModelUnloaded, data)
	}
	a.log.Info("models unloaded after a long pause", "paused", act.PausedFor.Round(time.Second), "models", len(loaded))
	a.sendHeartbeat(ctx)
}

// notifyStreams tells the hub about every request in flight on the live session.
func (a *Agent) notifyStreams(state string, reasons []string) int {
	a.emitMu.Lock()
	s := a.live
	a.emitMu.Unlock()
	if s == nil {
		return 0
	}
	ids := s.streamIDs()
	for _, id := range ids {
		a.sendProxy(s, protocol.TypeProxyStatus, protocol.ProxyStatus{StreamID: id, State: state, Reasons: reasons})
	}
	return len(ids)
}

// isIdle reports whether the node has nothing to do: nothing is loaded or loading, no benchmark
// runs, no resident waits for a reload and the machine is not paused.
func (a *Agent) isIdle(cfg *protocol.NodeConfig) bool {
	if a.isPaused() || a.selfActivity() || hasAutoResidents(cfg) {
		return false
	}
	_, _, r := a.config()
	if r == nil {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	loaded, err := r.Loaded(ctx)
	return err == nil && len(loaded) == 0
}

// idleBackoff is the factor for polling intervals while the node is idle (energy profile,
// E2-05 E: the performance profile never backs off).
func (a *Agent) idleBackoff(cfg *protocol.NodeConfig) int {
	if f := cfg.EffectivePower().IdleBackoff; f > 1 && a.isIdle(cfg) {
		return f
	}
	return 1
}
