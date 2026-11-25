package agent

import (
	"bytes"
	"context"
	"encoding/json"
	"sort"
	"strconv"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/runner"
)

// Action names.
const (
	ActionModelsList        = "models.list"
	ActionModelsLoad        = "models.load"
	ActionModelsUnload      = "models.unload"
	ActionModelsDownload    = "models.download"
	ActionModelsDelete      = "models.delete"
	ActionCapabilityInvoke  = "capability.invoke"
	ActionCapabilityExecute = "capability.execute"
	ActionNodeBenchmark     = "node.benchmark"
	ActionNodeSleep         = "node.sleep"
	ActionModelsSend        = "models.send"
	ActionModelsReceive     = "models.receive"
	// ActionWorkshopManage runs one management capability of the dev workshop (create a local
	// repository, branch, merge, publish …) that the person started on the hub's page (D76).
	ActionWorkshopManage = "workshop.manage"
	// ActionBuildManage runs one step of the build service (NS-01): a capability named build.*
	// that the admin started on the hub's Releases page. Nothing else runs it.
	ActionBuildManage = "build.manage"
	// ActionHubManage runs the roll-out of the hub on the production host (NS-01, phase 5): the
	// capability hub.update, started by the admin's own click on the Releases page.
	ActionHubManage = "hub.manage"
)

// (ActionNodeUpdate, "node.update", is declared with the update channel in updates.go.)

// knownNotImplemented are protocol actions this agent version does not implement yet.
var knownNotImplemented = map[string]bool{}

type actionFunc func(ctx context.Context, args map[string]json.RawMessage) (result any, perr *protocol.Error, followUp func())

func (a *Agent) actionHandler(name string) (actionFunc, bool) {
	switch name {
	case ActionModelsList:
		return a.actModelsList, true
	case ActionModelsLoad:
		return a.actModelsLoad, true
	case ActionModelsUnload:
		return a.actModelsUnload, true
	case ActionModelsDownload:
		return a.actModelsDownload, true
	case ActionModelsDelete:
		return a.actModelsDelete, true
	case ActionModelsSend:
		return a.actModelsSend, true
	case ActionModelsReceive:
		return a.actModelsReceive, true
	case ActionNodeBenchmark:
		return a.actNodeBenchmark, true
	case ActionNodeSleep:
		return a.actNodeSleep, true
	case ActionNodeUpdate:
		return a.actNodeUpdate, true
	case ActionCapabilityInvoke:
		return a.actCapabilityInvoke, true
	case ActionCapabilityExecute:
		return a.actCapabilityExecute, true
	case ActionWorkshopManage:
		return a.actWorkshopManage, true
	case ActionBuildManage:
		return a.actBuildManage, true
	case ActionHubManage:
		return a.actHubManage, true
	}
	return nil, false
}

// handleAction executes an action and replies with action_result on the session it came from.
func (a *Agent) handleAction(ctx context.Context, s *session, env protocol.Envelope) {
	// An action does not outlive its session (background jobs like a model load do).
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		select {
		case <-s.done:
			cancel()
		case <-ctx.Done():
		}
	}()
	var act protocol.Action
	var res protocol.ActionResult
	var follow func()
	if err := env.DecodeBody(&act); err != nil {
		res = protocol.ActionResult{OK: false, Error: errorf(protocol.ErrInvalidArgs, "malformed action: %v", err)}
	} else {
		result, perr, f := a.execute(ctx, act)
		follow = f
		if perr != nil {
			res = protocol.ActionResult{OK: false, Error: perr}
		} else {
			res = protocol.ActionResult{OK: true, Result: result}
		}
	}
	log := a.log.With("action", act.Name, "id", env.ID)
	if res.OK {
		log.Info("action done")
	} else {
		log.Warn("action failed", "code", res.Error.Code, "message", res.Error.Message)
	}
	out, err := protocol.New(protocol.TypeActionResult, res, a.opts.Now())
	if err != nil {
		log.Error("build action_result", "err", err)
		return
	}
	out.Re = env.ID
	if err := s.write(out); err != nil {
		log.Warn("could not send action_result", "err", err)
	}
	if follow != nil {
		follow()
	}
}

// execute checks the whitelist and dispatches to the handler.
func (a *Agent) execute(ctx context.Context, act protocol.Action) (any, *protocol.Error, func()) {
	cfg, _, _ := a.config()
	if cfg == nil || !cfg.ActionAllowed(act.Name) {
		return nil, errorf(protocol.ErrNotAllowed, "action %s is not allowed on this node", act.Name), nil
	}
	if knownNotImplemented[act.Name] {
		return nil, errorf(protocol.ErrNotImplemented, "action %s is not implemented in agent version %s", act.Name, a.opts.AgentVersion), nil
	}
	h, ok := a.actionHandler(act.Name)
	if !ok {
		return nil, errorf(protocol.ErrNotAllowed, "unknown action %s", act.Name), nil
	}
	if act.Args == nil {
		act.Args = map[string]json.RawMessage{}
	}
	return h(ctx, act.Args)
}

func (a *Agent) runner() (runner.ModelRunner, *protocol.Error) {
	_, _, r := a.config()
	if r == nil {
		return nil, errorf(protocol.ErrRunnerError, "no runner configured")
	}
	return r, nil
}

func (a *Agent) actModelsList(ctx context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args); perr != nil {
		return nil, perr, nil
	}
	r, perr := a.runner()
	if perr != nil {
		return nil, perr, nil
	}
	models, err := r.ListModels(ctx)
	if err != nil {
		return nil, errorf(protocol.ErrRunnerError, "list models: %v", err), nil
	}
	loaded, err := r.Loaded(ctx)
	if err != nil {
		return nil, errorf(protocol.ErrRunnerError, "list loaded models: %v", err), nil
	}
	pm := make([]protocol.Model, 0, len(models))
	for _, m := range models {
		pm = append(pm, protocol.Model{Name: m.Name, SizeBytes: nonNeg(m.SizeBytes), Format: m.Format})
	}
	pl := make([]protocol.LoadedModel, 0, len(loaded))
	for _, l := range loaded {
		pl = append(pl, toProtoLoaded(l))
	}
	return map[string]any{"models": pm, "loaded": pl}, nil, func() { a.sendInventory(ctx) }
}

func (a *Agent) actModelsLoad(ctx context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args, "model", "context_length", "min_context_length", "n_parallel", "ttl_seconds"); perr != nil {
		return nil, perr, nil
	}
	model, ok := argString(args, "model")
	if !ok || model == "" {
		return nil, errorf(protocol.ErrInvalidArgs, "args.model must be a non-empty string"), nil
	}
	var opts runner.LoadOptions
	if raw, present := args["context_length"]; present {
		n, ok := parseInt(raw)
		if !ok || n < 1 || n > 1<<31-1 {
			return nil, errorf(protocol.ErrInvalidArgs, "args.context_length must be an integer >= 1"), nil
		}
		opts.ContextLength = int(n)
	}
	if raw, present := args["min_context_length"]; present {
		n, ok := parseInt(raw)
		if !ok || n < 1 || n > 1<<31-1 {
			return nil, errorf(protocol.ErrInvalidArgs, "args.min_context_length must be an integer >= 1"), nil
		}
		opts.MinContext = int(n)
	}
	if raw, present := args["n_parallel"]; present {
		n, ok := parseInt(raw)
		if !ok || n < 1 || n > protocol.MaxParallel {
			return nil, errorf(protocol.ErrInvalidArgs, "args.n_parallel must be an integer 1..%d", protocol.MaxParallel), nil
		}
		opts.NParallel = int(n)
	}
	if raw, present := args["ttl_seconds"]; present {
		n, ok := parseInt(raw)
		if !ok || n < 0 || n > int64(365*24*3600) {
			return nil, errorf(protocol.ErrInvalidArgs, "args.ttl_seconds must be an integer >= 0"), nil
		}
		opts.TTL = time.Duration(n) * time.Second
	}
	if a.isGaming() {
		return nil, errorf(protocol.ErrBusy, "node is busy gaming; models cannot be loaded now"), nil
	}
	if a.isPaused() {
		return nil, errorf(protocol.ErrBusy, "node is paused for local use; models cannot be loaded now"), nil
	}
	if opts.ContextLength == 0 {
		if configured := a.configuredContext(model); opts.MinContext > 0 {
			// "at least": what this node's own config would have used is part of the floor, and
			// the runner sizes the rest (an explicit context_length would have won over all)
			opts.MinContext = max(opts.MinContext, configured)
		} else {
			opts.ContextLength = configured
		}
	}
	if opts.NParallel == 0 {
		opts.NParallel = a.configuredParallel(model)
	}
	opts.NoJinja = a.configuredNoJinja(model)
	r, perr := a.runner()
	if perr != nil {
		return nil, perr, nil
	}
	if loaded, err := r.Loaded(ctx); err == nil {
		for _, l := range loaded {
			if l.Name == model {
				a.setTTLIfGiven(model, opts.TTL)
				return map[string]any{"loaded": toProtoLoaded(l)}, nil, nil // already there: nothing to report
			}
		}
	}
	finish := func(j *loadJob, lm runner.LoadedModel, err error) (runner.LoadedModel, *protocol.Error) {
		bctx := a.jobCtx()
		if err != nil {
			perr := loadError(model, err)
			a.reportLoadFailure(j, model, perr)
			a.sendHeartbeat(bctx)
			return lm, perr
		}
		// A game may have started while the model was loading: give the VRAM back.
		if a.isGaming() {
			if err := r.Unload(context.WithoutCancel(bctx), model); err != nil {
				a.log.Error("unload after game start", "model", model, "err", err)
			}
			perr := errorf(protocol.ErrBusy, "a game started while loading %s; it was unloaded again", model)
			a.reportLoadFailure(j, model, perr)
			a.sendHeartbeat(bctx)
			return lm, perr
		}
		a.touch(model)
		a.setTTL(model, opts.TTL)
		a.press.ManualLoad(model)
		data := map[string]any{"model": lm.Name}
		if lm.VRAMUsedMB > 0 {
			data["vram_used_mb"] = lm.VRAMUsedMB
		}
		a.emit(protocol.EventModelLoaded, data)
		a.sendHeartbeat(bctx)
		return lm, nil
	}
	job, owner := a.startLoad(r, model, opts, finish)
	a.sendHeartbeat(ctx) // the hub routes requests for this model here instead of loading it elsewhere
	select {
	case <-job.done:
	case <-ctx.Done():
		// The load goes on; its outcome arrives as a model_loaded / model_load_failed event.
		return nil, errorf(protocol.ErrTimeout, "the load of %s is still running; the result is reported as an event", model), nil
	}
	if job.perr != nil {
		return nil, job.perr, nil
	}
	if !owner {
		a.setTTLIfGiven(model, opts.TTL)
	}
	return map[string]any{"loaded": toProtoLoaded(job.lm)}, nil, nil
}

// setTTLIfGiven applies a ttl_seconds of a request that did not start the load itself.
func (a *Agent) setTTLIfGiven(model string, ttl time.Duration) {
	if ttl > 0 {
		a.setTTL(model, ttl)
	}
}

// jobCtx is the context of background work that outlives a request (never nil).
func (a *Agent) jobCtx() context.Context {
	if c := a.ctx(); c != nil {
		return c
	}
	return context.Background()
}

func (a *Agent) actModelsUnload(ctx context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args, "model", "all"); perr != nil {
		return nil, perr, nil
	}
	_, hasModel := args["model"]
	_, hasAll := args["all"]
	if hasModel == hasAll {
		return nil, errorf(protocol.ErrInvalidArgs, "exactly one of args.model or args.all=true is required"), nil
	}
	r, perr := a.runner()
	if perr != nil {
		return nil, perr, nil
	}
	if hasAll {
		all, ok := parseBool(args["all"])
		if !ok || !all {
			return nil, errorf(protocol.ErrInvalidArgs, "args.all must be true"), nil
		}
		a.cancelLoads(ctx, "", "unload_all") // a model that is still loading must not appear after "unload all"
		unloaded, err := r.UnloadAll(ctx)
		if err != nil {
			return nil, errorf(protocol.ErrRunnerError, "unload all: %v", err), nil
		}
		names := make([]string, 0, len(unloaded))
		for _, u := range unloaded {
			names = append(names, u.Name)
			a.forget(u.Name)
			a.press.ManualUnload(u.Name)
		}
		return map[string]any{"unloaded": names}, nil, func() {
			for _, u := range unloaded {
				a.emit(protocol.EventModelUnloaded, unloadedData(u, "action"))
			}
			a.sendHeartbeat(ctx)
		}
	}
	model, ok := argString(args, "model")
	if !ok || model == "" {
		return nil, errorf(protocol.ErrInvalidArgs, "args.model must be a non-empty string"), nil
	}
	a.cancelLoads(ctx, model, "unload")
	before := runner.LoadedModel{Name: model}
	if loaded, err := r.Loaded(ctx); err == nil {
		for _, l := range loaded {
			if l.Name == model {
				before = l
			}
		}
	}
	if err := r.Unload(ctx, model); err != nil {
		return nil, errorf(protocol.ErrRunnerError, "unload %s: %v", model, err), nil
	}
	a.forget(model)
	a.press.ManualUnload(model)
	return map[string]any{"unloaded": []string{model}}, nil, func() {
		a.emit(protocol.EventModelUnloaded, unloadedData(before, "action"))
		a.sendHeartbeat(ctx)
	}
}

func unloadedData(l runner.LoadedModel, reason string) map[string]any {
	d := map[string]any{"model": l.Name, "reason": reason}
	if l.VRAMUsedMB > 0 {
		d["vram_used_mb"] = l.VRAMUsedMB
	}
	return d
}

// touch records that a model was used now (for idle unload).
func (a *Agent) touch(model string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.lastUsed[model] = a.opts.Now()
}

func (a *Agent) forget(model string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	delete(a.lastUsed, model)
	delete(a.ttl, model)
}

// setTTL records a per-model idle TTL (0 = use config.idle_ttl_seconds).
func (a *Agent) setTTL(model string, ttl time.Duration) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if ttl > 0 {
		a.ttl[model] = ttl
	} else {
		delete(a.ttl, model)
	}
}

// --- argument parsing -------------------------------------------------------

func onlyKeys(args map[string]json.RawMessage, allowed ...string) *protocol.Error {
	var unknown []string
	for k := range args {
		ok := false
		for _, a := range allowed {
			if k == a {
				ok = true
				break
			}
		}
		if !ok {
			unknown = append(unknown, k)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return errorf(protocol.ErrInvalidArgs, "unknown args: %v", unknown)
	}
	return nil
}

func argString(args map[string]json.RawMessage, key string) (string, bool) {
	raw, ok := args[key]
	if !ok {
		return "", false
	}
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 || raw[0] != '"' {
		return "", false
	}
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return "", false
	}
	return s, true
}

// parseInt accepts only plain JSON integers (no fractions, exponents, strings).
func parseInt(raw json.RawMessage) (int64, bool) {
	n, err := strconv.ParseInt(string(bytes.TrimSpace(raw)), 10, 64)
	return n, err == nil
}

func parseBool(raw json.RawMessage) (bool, bool) {
	switch string(bytes.TrimSpace(raw)) {
	case "true":
		return true, true
	case "false":
		return false, true
	}
	return false, false
}
