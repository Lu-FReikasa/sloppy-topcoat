package agent

import (
	"context"
	"strings"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/runner"
	"topcoat/node/internal/providers/watcher"
)

func (a *Agent) watcherRules() ([]watcher.Rule, time.Duration) {
	cfg, _, _ := a.config()
	if cfg == nil {
		return nil, 0
	}
	rules := make([]watcher.Rule, 0, len(cfg.Watcher.Rules))
	for _, r := range cfg.Watcher.Rules {
		rules = append(rules, watcher.Rule{Process: r.Process, PathPrefix: r.PathPrefix})
	}
	return rules, time.Duration(cfg.Watcher.GraceSeconds) * time.Second
}

func (a *Agent) cooldown() time.Duration {
	cfg, _, _ := a.config()
	if cfg == nil {
		return protocol.DefaultCooldownSeconds * time.Second
	}
	return time.Duration(cfg.Watcher.Cooldown()) * time.Second
}

func sameRules(a, b []watcher.Rule) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if !strings.EqualFold(a[i].Process, b[i].Process) || !strings.EqualFold(a[i].PathPrefix, b[i].PathPrefix) {
			return false
		}
	}
	return true
}

// watcherLoop (re)starts the process watcher whenever the rules change and
// reacts to its events. It runs independently of the hub session.
func (a *Agent) watcherLoop(ctx context.Context) {
	var (
		cancel  context.CancelFunc
		events  <-chan watcher.Event
		current []watcher.Rule
		started bool
	)
	stop := func() {
		if cancel != nil {
			cancel()
			cancel = nil
		}
		events = nil
	}
	defer stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-a.cfgChanged:
			rules, _ := a.watcherRules()
			if started && sameRules(rules, current) {
				continue
			}
			stop()
			started = true
			current = rules
			a.endGamesNotMatching(ctx, rules)
			if len(rules) == 0 {
				continue
			}
			wctx, c := context.WithCancel(ctx)
			ch, err := a.opts.Watcher.Watch(wctx, rules)
			if err != nil {
				c()
				a.log.Warn("process watcher unavailable; games will not be detected", "err", err)
				continue
			}
			cancel, events = c, ch
			a.log.Info("process watcher started", "rules", len(rules))
		case ev, ok := <-events:
			if !ok {
				events = nil
				continue
			}
			a.handleProcessEvent(ctx, ev)
		}
	}
}

// endGamesNotMatching ends games whose rule was removed from the config.
func (a *Agent) endGamesNotMatching(ctx context.Context, rules []watcher.Rule) {
	a.mu.Lock()
	var gone []watcher.Event
	for _, ev := range a.gaming {
		match := false
		for _, r := range rules {
			if r.Matches(ev.Process, ev.Path) {
				match = true
				break
			}
		}
		if !match {
			gone = append(gone, ev)
		}
	}
	a.mu.Unlock()
	for _, ev := range gone {
		ev.Started = false
		a.handleProcessEvent(ctx, ev)
	}
}

// handleProcessEvent reacts to a watched process starting or ending. It is
// called from the watcher goroutine only, so the check-then-act on the gaming
// set is not racy. game_detected / game_ended are emitted before the gaming set
// changes so that a concurrent heartbeat cannot report the resulting
// state_changed ahead of the game event that caused it.
//
// After the last game ended the node stays busy_gaming for the configured
// cooldown (default 60 s) so that a game restart or a launcher hopping between
// processes does not immediately trigger new loads.
func (a *Agent) handleProcessEvent(ctx context.Context, ev watcher.Event) {
	key := ev.Key()
	_, grace := a.watcherRules()
	a.mu.Lock()
	_, running := a.gaming[key]
	a.mu.Unlock()
	if ev.Started == running {
		return // duplicate start, or end of a process we never saw start
	}
	data := map[string]any{"process": ev.Process}
	if ev.Path != "" {
		data["path"] = ev.Path
	}
	if ev.Started {
		a.log.Info("game detected", "process", ev.Process, "path", ev.Path, "unload_after", grace)
		a.emit(protocol.EventGameDetected, data)
		a.abortBenchmark("game detected")
		a.abortTransfer("game detected")
		a.mu.Lock()
		first := len(a.gaming) == 0
		a.gaming[key] = ev
		if a.coolStop != nil {
			a.coolStop()
			a.coolStop = nil
		}
		var gctx context.Context
		if first {
			var cancel context.CancelFunc
			gctx, cancel = context.WithCancel(ctx)
			a.gameStop = cancel
		}
		a.mu.Unlock()
		a.refreshState(ctx)
		a.sendHeartbeat(ctx)
		if first {
			a.bg.Add(1)
			go func() {
				defer a.bg.Done()
				a.unloadForGame(gctx, grace)
			}()
		}
		return
	}
	a.log.Info("game ended", "process", ev.Process, "path", ev.Path)
	a.emit(protocol.EventGameEnded, data)
	cool := a.cooldown()
	a.mu.Lock()
	delete(a.gaming, key)
	last := len(a.gaming) == 0
	if last && a.gameStop != nil {
		a.gameStop()
		a.gameStop = nil
	}
	var cctx context.Context
	if last && cool > 0 {
		var cancel context.CancelFunc
		cctx, cancel = context.WithCancel(ctx)
		a.cooling = true
		a.coolStop = cancel
	}
	a.mu.Unlock()
	if !last {
		return
	}
	if cool <= 0 {
		a.refreshState(ctx)
		a.sendHeartbeat(ctx)
		return
	}
	a.log.Info("staying busy during cooldown", "cooldown", cool)
	a.bg.Add(1)
	go func() {
		defer a.bg.Done()
		select {
		case <-cctx.Done():
			return
		case <-a.opts.After(cool):
		}
		a.mu.Lock()
		if cctx.Err() != nil { // a game started again meanwhile
			a.mu.Unlock()
			return
		}
		a.cooling = false
		a.coolStop = nil
		a.mu.Unlock()
		a.refreshState(ctx)
		a.sendHeartbeat(ctx)
	}()
}

// unloadForGame waits for the grace period (canceled if the game ends first) and unloads all models.
func (a *Agent) unloadForGame(gctx context.Context, grace time.Duration) {
	if grace > 0 {
		select {
		case <-gctx.Done():
			return
		case <-a.opts.After(grace):
		}
	}
	if gctx.Err() != nil {
		return
	}
	a.unloadAll(context.WithoutCancel(gctx), "game")
}

func (a *Agent) unloadAll(ctx context.Context, reason string) {
	_, _, r := a.config()
	if r == nil {
		return
	}
	unloaded, err := r.UnloadAll(ctx)
	if err != nil {
		a.log.Error("unload all models", "reason", reason, "err", err)
		a.emit(protocol.EventError, map[string]any{"message": "unload all (" + reason + ") failed: " + err.Error()})
		return
	}
	if reason == "game" {
		names := make([]string, 0, len(unloaded))
		for _, u := range unloaded {
			names = append(names, u.Name)
		}
		a.press.GameUnloaded(a.opts.Now(), names...)
	}
	for _, u := range unloaded {
		a.forget(u.Name)
		a.log.Info("model unloaded", "model", u.Name, "reason", reason)
		a.emit(protocol.EventModelUnloaded, unloadedData(u, reason))
	}
	if len(unloaded) > 0 {
		a.sendHeartbeat(ctx)
	}
}

// idleLoop periodically unloads models that were not used for idle_ttl_seconds.
func (a *Agent) idleLoop(ctx context.Context) {
	t := time.NewTicker(a.opts.IdleCheckInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			a.checkIdle(ctx)
		}
	}
}

// checkIdle unloads models whose last use is older than the idle TTL. Models
// that were loaded outside the agent start their idle timer when first seen.
func (a *Agent) checkIdle(ctx context.Context) {
	cfg, _, r := a.config()
	if cfg == nil || r == nil {
		return
	}
	ttl := time.Duration(cfg.IdleTTLSeconds) * time.Second
	loaded, err := r.Loaded(ctx)
	if err != nil {
		return
	}
	now := a.opts.Now()
	var expired []string
	a.mu.Lock()
	vram := map[string]int64{}
	for _, l := range loaded {
		vram[l.Name] = l.VRAMUsedMB
		last, ok := a.lastUsed[l.Name]
		if !ok {
			a.lastUsed[l.Name] = now
			continue
		}
		if res, ok := cfg.Resident(l.Name); ok && res.Reloads() {
			continue // residents stay loaded; pressure handling unloads them when needed
		}
		limit := ttl
		if t, ok := a.ttl[l.Name]; ok {
			limit = t
		}
		if limit > 0 && now.Sub(last) >= limit {
			expired = append(expired, l.Name)
		}
	}
	for name := range a.lastUsed {
		if _, ok := vram[name]; !ok {
			delete(a.lastUsed, name)
		}
	}
	a.mu.Unlock()
	changed := false
	for _, name := range expired {
		if err := r.Unload(ctx, name); err != nil {
			a.log.Error("idle unload", "model", name, "err", err)
			continue
		}
		a.forget(name)
		changed = true
		a.log.Info("model unloaded", "model", name, "reason", "idle")
		a.emit(protocol.EventModelUnloaded, unloadedData(runner.LoadedModel{Name: name, VRAMUsedMB: vram[name]}, "idle"))
	}
	if changed {
		a.sendHeartbeat(ctx)
	}
}
