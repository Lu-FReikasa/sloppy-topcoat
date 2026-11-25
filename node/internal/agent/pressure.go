package agent

import (
	"context"
	"sort"
	"time"

	"topcoat/node/internal/pressure"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/gpu"
	"topcoat/node/internal/providers/runner"
)

// pressureLoop samples memory every pressure.poll_seconds and executes the
// pressure controller's decisions: yield models when other programs need the
// memory, reload residents when it is free again (E1-06). It runs
// independently of the hub session, like the process watcher.
func (a *Agent) pressureLoop(ctx context.Context) {
	for {
		t := time.NewTimer(a.pressurePoll())
		select {
		case <-ctx.Done():
			t.Stop()
			return
		case <-t.C:
		}
		a.pressureStep(ctx)
	}
}

func (a *Agent) pressurePoll() time.Duration {
	if a.opts.PressurePoll > 0 {
		return a.opts.PressurePoll
	}
	cfg, _, _ := a.config()
	if cfg == nil {
		return protocol.DefaultPressurePollSeconds * time.Second
	}
	return cfg.EffectivePressure().Poll * time.Duration(a.idleBackoff(cfg))
}

// hasAutoResidents reports whether the config lists residents to keep loaded.
func hasAutoResidents(cfg *protocol.NodeConfig) bool {
	for _, r := range cfg.Residents {
		if r.Reloads() {
			return true
		}
	}
	return false
}

// pressureStep takes one sample and executes at most one action.
func (a *Agent) pressureStep(ctx context.Context) {
	cfg, _, r := a.config()
	if cfg == nil || r == nil {
		return
	}
	p := cfg.EffectivePressure()
	// With nothing loaded and no resident waiting there is nothing to yield and nothing to
	// reload: the GPU is not asked at all (an idle node should not wake the driver every poll).
	if (p.Enabled || p.RAMEnabled || hasAutoResidents(cfg) || a.press.HasEvicted()) && !a.nothingToWatch(ctx, r, cfg) {
		s := a.pressureSample(ctx, r, cfg)
		if act := a.press.Step(s); act != nil {
			switch act.Kind {
			case pressure.Yield:
				a.yieldModel(ctx, r, *act)
			case pressure.Reload:
				a.reloadModel(ctx, r, *act, p)
			}
		}
	}
	a.syncPressureState(ctx)
}

// nothingToWatch is true when no model is loaded or loading and no resident is to be reloaded.
func (a *Agent) nothingToWatch(ctx context.Context, r runner.ModelRunner, cfg *protocol.NodeConfig) bool {
	if hasAutoResidents(cfg) || a.press.HasEvicted() || a.selfActivity() {
		return false
	}
	loaded, err := r.Loaded(ctx)
	return err == nil && len(loaded) == 0
}

// syncPressureState reports a change of the "yielded" state right away.
func (a *Agent) syncPressureState(ctx context.Context) {
	active := a.press.Active(a.opts.Now())
	a.mu.Lock()
	changed := active != a.yieldedState
	a.yieldedState = active
	a.mu.Unlock()
	if changed {
		a.refreshState(ctx)
		a.sendHeartbeat(ctx)
	}
}

// selfActivity is true while the node itself changes its memory use: a model
// is being loaded or a benchmark runs.
func (a *Agent) selfActivity() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.loading) > 0 || a.bench != nil
}

// beginLoad marks model as loading (heartbeat "loading", no pressure
// decisions meanwhile); the returned func ends it.
func (a *Agent) beginLoad(model string) func() {
	a.mu.Lock()
	if a.loading == nil {
		a.loading = map[string]int{}
	}
	a.loading[model]++
	a.mu.Unlock()
	return func() {
		a.mu.Lock()
		if a.loading[model]--; a.loading[model] <= 0 {
			delete(a.loading, model)
		}
		a.mu.Unlock()
	}
}

// loadingNames lists the models being loaded, sorted.
func (a *Agent) loadingNames() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	names := make([]string, 0, len(a.loading))
	for n := range a.loading {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

// discreteGPUs returns the indices of non-integrated GPUs and the one with the
// most VRAM (-1 if none).
func discreteGPUs(gpus []gpu.GPU) ([]int, int) {
	var idx []int
	primary := -1
	for i, g := range gpus {
		if g.Integrated {
			continue
		}
		idx = append(idx, i)
		if primary < 0 || g.VRAMTotalMB > gpus[primary].VRAMTotalMB {
			primary = i
		}
	}
	return idx, primary
}

// ownVRAM is our processes' dedicated VRAM on GPU i: measured where the
// platform can attribute usage, otherwise the loaded models' VRAM counted on
// the primary GPU.
func ownVRAM(i, primary int, u gpu.ProcessUsage, estimate int64) (int64, bool) {
	if u.OwnKnown {
		return u.OwnDedicatedMB, true
	}
	if i == primary {
		return estimate, false
	}
	return 0, false
}

func modelOnCPU(l runner.LoadedModel, haveGPU bool) bool {
	return l.Backend == protocol.BackendCPU || !haveGPU
}

func (a *Agent) pressureSample(ctx context.Context, r runner.ModelRunner, cfg *protocol.NodeConfig) pressure.Sample {
	s := pressure.Sample{Now: a.opts.Now(), RuleActive: a.isGaming(), SelfActivity: a.selfActivity()}
	loaded, err := r.Loaded(ctx)
	if err != nil {
		s.RunnerDown = true
	}
	pids := make([]int, 0, len(loaded))
	for _, l := range loaded {
		if l.PID > 0 {
			pids = append(pids, l.PID)
		}
	}
	gpus, usage, gerr := gpu.SampleOf(ctx, a.opts.GPU, pids)
	if gerr != nil {
		a.log.Debug("gpu sample", "err", gerr)
	}
	discrete, primary := discreteGPUs(gpus)
	haveGPU := len(discrete) > 0
	var estimate int64
	for _, l := range loaded {
		if !modelOnCPU(l, haveGPU) {
			estimate += l.VRAMUsedMB
		}
	}
	for _, i := range discrete {
		g, u := gpus[i], usageAt(usage, i)
		own, measured := ownVRAM(i, primary, u, estimate)
		s.GPUs = append(s.GPUs, pressure.GPU{
			TotalMB: g.VRAMTotalMB, UsedMB: g.VRAMUsedMB, OwnMB: own, OwnMeasured: measured,
			OwnSharedMB: u.OwnSharedMB, SharedMeasured: u.SharedKnown,
			ForeignUtil: u.ForeignUtilPercent, UtilKnown: u.ForeignUtilKnown,
			ForeignSharedMaxMB: u.ForeignSharedMaxMB, ForeignSharedKnown: u.ForeignSharedKnown,
		})
	}
	if info, err := a.opts.SysInfo.Info(ctx); err == nil {
		s.RAMTotalMB, s.RAMUsedMB = info.RAMTotalMB, info.RAMUsedMB
	}
	a.mu.Lock()
	for _, l := range loaded {
		s.Loaded = append(s.Loaded, pressure.Model{Name: l.Name, VRAMMB: l.VRAMUsedMB, RAMMB: l.RAMUsedMB,
			CPU: modelOnCPU(l, haveGPU), LastUsed: a.lastUsed[l.Name]})
	}
	a.mu.Unlock()
	if needsSizes(cfg, loaded, a.press.HasEvicted()) {
		if models, err := r.ListModels(ctx); err == nil {
			s.OnDisk = make(map[string]int64, len(models))
			for _, m := range models {
				s.OnDisk[m.Name] = m.SizeBytes >> 20
			}
		}
	}
	return s
}

func usageAt(u []gpu.ProcessUsage, i int) gpu.ProcessUsage {
	if i < len(u) {
		return u[i]
	}
	return gpu.ProcessUsage{}
}

// needsSizes reports whether a resident waits for a reload (the model list is
// only read then).
func needsSizes(cfg *protocol.NodeConfig, loaded []runner.LoadedModel, evicted bool) bool {
	isLoaded := map[string]bool{}
	for _, l := range loaded {
		isLoaded[l.Name] = true
	}
	for _, r := range cfg.Residents {
		if r.Reloads() && !isLoaded[r.Model] {
			return true
		}
	}
	return evicted

}

// yieldModel unloads a model because other programs need the memory.
func (a *Agent) yieldModel(ctx context.Context, r runner.ModelRunner, act pressure.Action) {
	before := runner.LoadedModel{Name: act.Model}
	if loaded, err := r.Loaded(ctx); err == nil {
		for _, l := range loaded {
			if l.Name == act.Model {
				before = l
			}
		}
	}
	if err := r.Unload(ctx, act.Model); err != nil {
		a.log.Error("yield model", "model", act.Model, "err", err)
		a.emit(protocol.EventError, map[string]any{"message": "unload " + act.Model + " (" + act.Reason + ") failed: " + err.Error()})
		return
	}
	a.forget(act.Model)
	until, paused := a.press.Yielded(act.Model, a.opts.Now())
	a.log.Info("model unloaded", "model", act.Model, "reason", act.Reason, "signal", act.Signal, "free_mb", act.FreeMB)
	data := unloadedData(before, act.Reason)
	data["free_mb"] = act.FreeMB
	if act.Signal != "" {
		data["signal"] = act.Signal
	}
	a.emit(protocol.EventModelUnloaded, data)
	if paused {
		a.log.Warn("automatic reload paused (model yielded too often)", "model", act.Model, "until", until)
		a.emit(protocol.EventAutoReloadPaused, map[string]any{"model": act.Model, "until": protocol.FormatTS(until), "yields": pressure.FlapLimit})
	}
	a.syncPressureState(ctx)
	a.sendHeartbeat(ctx)
}

// reloadModel loads a resident model again on the controller's request.
func (a *Agent) reloadModel(ctx context.Context, r runner.ModelRunner, act pressure.Action, p protocol.Pressure) {
	a.log.Info("loading resident model", "model", act.Model, "reason", act.Reason, "free_mb", act.FreeMB)
	ctxLen := act.ContextLength
	if ctxLen == 0 {
		ctxLen = a.configuredContext(act.Model)
	}
	finish := func(_ *loadJob, lm runner.LoadedModel, err error) (runner.LoadedModel, *protocol.Error) {
		if err != nil {
			perr := loadError(act.Model, err)
			if perr.Code != protocol.ErrCanceled { // canceled = someone unloaded it on purpose
				a.press.ReloadFailed(act.Model, a.opts.Now())
				a.log.Error("automatic load failed", "model", act.Model, "err", err)
				a.emit(protocol.EventError, map[string]any{"message": "automatic load of " + act.Model + " failed: " + err.Error()})
			}
			return lm, perr
		}
		// A watched game may have started while the model was loading.
		if a.isGaming() && !p.ReloadWhileRuleMatches {
			if err := r.Unload(context.WithoutCancel(a.jobCtx()), act.Model); err != nil {
				a.log.Error("unload after game start", "model", act.Model, "err", err)
			}
			return lm, errorf(protocol.ErrBusy, "a game started while loading %s; it was unloaded again", act.Model)
		}
		a.touch(act.Model)
		a.press.Reloaded(act.Model, lm.VRAMUsedMB, lm.RAMUsedMB)
		data := map[string]any{"model": lm.Name, "reason": act.Reason}
		if lm.VRAMUsedMB > 0 {
			data["vram_used_mb"] = lm.VRAMUsedMB
		}
		a.emit(protocol.EventModelLoaded, data)
		return lm, nil
	}
	job, owner := a.startLoad(r, act.Model, runner.LoadOptions{ContextLength: ctxLen, NParallel: a.configuredParallel(act.Model), NoJinja: a.configuredNoJinja(act.Model)}, finish)
	select {
	case <-job.done:
	case <-ctx.Done():
		return
	}
	if !owner {
		return // a manual load of the same model was running; it reports itself
	}
	a.syncPressureState(ctx)
	a.sendHeartbeat(ctx)
}

// foreignUsage returns other programs' VRAM and utilization for a heartbeat.
func foreignUsage(gpus []gpu.GPU, usage []gpu.ProcessUsage, loaded []runner.LoadedModel) map[int]protocol.GPUUsage {
	discrete, primary := discreteGPUs(gpus)
	var estimate int64
	for _, l := range loaded {
		if !modelOnCPU(l, len(discrete) > 0) {
			estimate += l.VRAMUsedMB
		}
	}
	out := map[int]protocol.GPUUsage{}
	for _, i := range discrete {
		u := usageAt(usage, i)
		var fu protocol.GPUUsage
		if own, measured := ownVRAM(i, primary, u, estimate); measured || i == primary {
			v := max(gpus[i].VRAMUsedMB-own, 0)
			fu.ForeignVRAMUsedMB = &v
		}
		if u.ForeignUtilKnown {
			v := min(max(u.ForeignUtilPercent, 0), 100)
			fu.ForeignGPUUtil = &v
		}
		if u.ForeignSharedKnown {
			v := max(u.ForeignSharedMaxMB, 0)
			fu.ForeignSharedMaxMB = &v
		}
		out[i] = fu
	}
	return out
}
