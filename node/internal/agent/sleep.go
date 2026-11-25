package agent

import (
	"context"
	"encoding/json"
	"time"

	"topcoat/node/internal/protocol"
)

// Sleeping (E2-05 E): a node that is only needed now and then can put its machine to sleep and
// be woken by the hub (Wake-on-LAN, addresses from the inventory). Two ways in:
//   - the action node.sleep (not in the default allowed_actions: the admin adds it), and
//   - config.power.sleep_after_idle_seconds, the node sleeps by itself after that much idle time.
// Both refuse while there is anything the node is responsible for.

// busyReason names what stops the node from sleeping ("" = nothing).
func (a *Agent) busyReason() string {
	a.emitMu.Lock()
	live := a.live
	a.emitMu.Unlock()
	a.mu.Lock()
	downloads, bench := len(a.downloads), a.bench != nil
	loading := len(a.loading)
	a.mu.Unlock()
	switch {
	case a.isGaming():
		return "a game is running"
	case loading > 0:
		return "a model is loading"
	case bench:
		return "a benchmark is running"
	case downloads > 0:
		return "a download is running"
	case live != nil && len(live.streamIDs()) > 0:
		return "an answer is being generated"
	}
	return ""
}

func (a *Agent) actNodeSleep(ctx context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args); perr != nil {
		return nil, perr, nil
	}
	if a.opts.Sleeper == nil {
		return nil, errorf(protocol.ErrNotImplemented, "this node cannot put its machine to sleep"), nil
	}
	if why := a.busyReason(); why != "" {
		return nil, errorf(protocol.ErrBusy, "the node does not sleep now: %s", why), nil
	}
	delay := a.opts.SleepDelay
	if delay <= 0 {
		delay = 3 * time.Second
	}
	return map[string]any{"sleeping_in_seconds": int(delay.Seconds())}, nil, func() {
		go func() {
			// The result is on its way; the machine sleeps a moment later. Models are unloaded
			// first: their processes would be frozen by the sleep with GBs held in memory.
			a.emit(protocol.EventSleeping, map[string]any{"reason": "action"})
			a.sleepNow(a.jobCtx(), delay)
		}()
	}
}

// sleepNow unloads the models, waits delay and puts the machine to sleep.
func (a *Agent) sleepNow(ctx context.Context, delay time.Duration) {
	if _, _, r := a.config(); r != nil {
		if loaded, err := r.Loaded(ctx); err == nil && len(loaded) > 0 {
			if _, err := r.UnloadAll(ctx); err != nil {
				a.log.Warn("unload before sleep", "err", err)
			}
		}
	}
	select {
	case <-ctx.Done():
		return
	case <-time.After(delay):
	}
	if err := a.opts.Sleeper.Sleep(ctx); err != nil {
		a.log.Error("could not put the machine to sleep", "err", err)
		a.emit(protocol.EventError, map[string]any{"message": "sleep failed: " + err.Error()})
	}
}

func (a *Agent) sleepCheck() time.Duration {
	if a.opts.SleepCheck > 0 {
		return a.opts.SleepCheck
	}
	return 30 * time.Second
}

// sleepLoop puts the machine to sleep after config.power.sleep_after_idle_seconds of idleness:
// nothing loaded, no work, and nobody at the machine for that long.
func (a *Agent) sleepLoop(ctx context.Context) {
	var idleSince time.Time
	for {
		t := time.NewTimer(a.sleepCheck())
		select {
		case <-ctx.Done():
			t.Stop()
			return
		case <-t.C:
		}
		if a.opts.Sleeper == nil {
			continue
		}
		cfg, _, _ := a.config()
		if cfg == nil {
			continue
		}
		after := cfg.EffectivePower().SleepAfterIdle
		if after <= 0 || !a.isIdle(cfg) || a.busyReason() != "" || a.userIsHere(ctx, cfg, after) {
			idleSince = time.Time{}
			continue
		}
		now := a.opts.Now()
		if idleSince.IsZero() {
			idleSince = now
		}
		if now.Sub(idleSince) < after {
			continue
		}
		a.log.Info("idle: putting the machine to sleep", "idle", now.Sub(idleSince).Round(time.Second))
		a.emit(protocol.EventSleeping, map[string]any{"reason": "idle", "idle_seconds": int(now.Sub(idleSince).Seconds())})
		a.sleepNow(ctx, 2*time.Second)
		idleSince = time.Time{} // after waking up the clock starts again
	}
}

// userIsHere is true when keyboard or mouse input is younger than d (an unknown input counts as
// nobody there: the node then sleeps on idleness alone).
func (a *Agent) userIsHere(ctx context.Context, _ *protocol.NodeConfig, d time.Duration) bool {
	if a.opts.Activity == nil {
		return false
	}
	idle, known := a.opts.Activity.Input(ctx)
	return known && idle < d
}
