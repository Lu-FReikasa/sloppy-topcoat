package agent

import (
	"context"
	"errors"
	"sync/atomic"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/runner"
)

// A load of a big model takes 30 s to 2.5 min. It therefore runs as a job of its own, detached
// from the action (and the hub session) that asked for it:
//   - a second request for the same model joins the running job instead of starting another
//     (an action plus a chat request used to produce three loads, three events),
//   - "unload" / "unload all" cancel the job (the half-started server is stopped) instead of
//     letting the model appear after the user asked to unload everything,
//   - the outcome is reported as an event (model_loaded / model_load_failed) even when the hub
//     gave up waiting for the action result long before.
type loadJob struct {
	done   chan struct{}
	cancel context.CancelFunc
	// canceled is set by cancelLoads before it cancels; reason says why (unload, unload_all, ...).
	canceled atomic.Bool
	reason   atomic.Value // string
	started  time.Time
	// lm and perr are set before done is closed.
	lm   runner.LoadedModel
	perr *protocol.Error
}

// loadFinish runs once, in the job, after the runner returned. It may turn a successful load into
// an error (a game started meanwhile) and reports the outcome as events.
type loadFinish func(j *loadJob, lm runner.LoadedModel, err error) (runner.LoadedModel, *protocol.Error)

// startLoad returns the load of model that is already running (owner false), or starts one.
func (a *Agent) startLoad(r runner.ModelRunner, model string, opts runner.LoadOptions, finish loadFinish) (job *loadJob, owner bool) {
	a.mu.Lock()
	if j, ok := a.loads[model]; ok {
		a.mu.Unlock()
		return j, false
	}
	base := a.baseCtx
	if base == nil {
		base = context.Background()
	}
	jctx, cancel := context.WithCancel(base)
	job = &loadJob{done: make(chan struct{}), cancel: cancel, started: a.opts.Now()}
	if a.loads == nil {
		a.loads = map[string]*loadJob{}
	}
	a.loads[model] = job
	a.mu.Unlock()

	end := a.beginLoad(model)
	go func() {
		lm, err := r.Load(jctx, model, opts)
		if err == nil && job.canceled.Load() {
			// The load finished in the moment it was canceled: it must not stay loaded.
			if uerr := r.Unload(context.WithoutCancel(jctx), model); uerr != nil {
				a.log.Warn("unload a load that finished while it was canceled", "model", model, "err", uerr)
			}
			lm, err = runner.LoadedModel{}, context.Canceled
		}
		end()
		var perr *protocol.Error
		if finish != nil {
			lm, perr = finish(job, lm, err)
		} else if err != nil {
			perr = loadError(model, err)
		}
		cancel()
		job.lm, job.perr = lm, perr
		a.mu.Lock()
		delete(a.loads, model)
		a.mu.Unlock()
		close(job.done)
	}()
	return job, true
}

// loadError maps a runner failure to a protocol error.
func loadError(model string, err error) *protocol.Error {
	code := protocol.ErrRunnerError
	switch {
	case errors.Is(err, runner.ErrInsufficientMemory):
		code = protocol.ErrInsufficientMemory
	case errors.Is(err, context.Canceled):
		code = protocol.ErrCanceled
	}
	return errorf(code, "load %s: %v", model, err)
}

// reportLoadFailure emits model_load_failed: the reason an action result may never deliver.
// A load that was canceled on purpose is no failure: it is reported as EventModelLoadCancelled.
func (a *Agent) reportLoadFailure(j *loadJob, model string, perr *protocol.Error) {
	if perr.Code == protocol.ErrCanceled {
		reason, _ := j.reason.Load().(string)
		a.log.Info("model load canceled", "model", model, "reason", reason)
		a.emit(protocol.EventModelLoadCancelled, map[string]any{
			"model": model, "reason": reason, "after_seconds": int(a.opts.Now().Sub(j.started).Seconds()),
		})
		return
	}
	a.log.Warn("model load failed", "model", model, "code", perr.Code, "message", perr.Message)
	a.emit(protocol.EventModelLoadFailed, map[string]any{"model": model, "code": perr.Code, "message": perr.Message})
}

// cancelWait bounds how long an unload waits for a canceled load to be over: the runner kills
// the half-started server at once, the operating system may need a while to tear it down (the
// next start waits for that itself).
const cancelWait = 15 * time.Second

// cancelLoads cancels the load of model ("" = every load) for reason and waits (briefly) until
// the runner has killed the half-started server, so that a following unload sees the final state.
func (a *Agent) cancelLoads(ctx context.Context, model, reason string) {
	a.mu.Lock()
	var jobs []*loadJob
	for name, j := range a.loads {
		if model == "" || name == model {
			jobs = append(jobs, j)
		}
	}
	a.mu.Unlock()
	for _, j := range jobs {
		j.reason.Store(reason)
		j.canceled.Store(true)
		j.cancel()
	}
	wait := time.NewTimer(cancelWait)
	defer wait.Stop()
	for _, j := range jobs {
		select {
		case <-j.done:
		case <-ctx.Done():
			return
		case <-wait.C:
			a.log.Warn("a canceled load did not end in time")
			return
		}
	}
}
