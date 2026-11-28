package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"time"

	"topcoat/node/internal/bench"
	"topcoat/node/internal/hf"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/runner"
	"topcoat/node/internal/voice"
)

// Long-running actions (docs/PROTOCOL.md "Long-running actions"): they answer
// at once and report progress through events.

// DownloadProgressInterval throttles download_progress events.
const DownloadProgressInterval = 2 * time.Second

// benchTimeout is the longest a whole benchmark may take; a var so tests can shorten it.
var benchTimeout = 2 * time.Hour

// maxBenchModels bounds one benchmark request.
const maxBenchModels = 8

// modelNameRe is the allowed model name for downloads (also a file name).
var modelNameRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

func (a *Agent) actModelsDownload(ctx context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args, "source", "target_name", "target_kind"); perr != nil {
		return nil, perr, nil
	}
	kind := "model"
	if _, present := args["target_kind"]; present {
		var ok bool
		if kind, ok = argString(args, "target_kind"); !ok || (kind != "model" && kind != voice.KindSTT && kind != voice.KindTTS) {
			return nil, errorf(protocol.ErrInvalidArgs, "args.target_kind must be model, stt or tts"), nil
		}
	}
	var src struct {
		Type     string `json:"type"`
		Repo     string `json:"repo"`
		File     string `json:"file"`
		Revision string `json:"revision"`
	}
	raw, ok := args["source"]
	if !ok || json.Unmarshal(raw, &src) != nil {
		return nil, errorf(protocol.ErrInvalidArgs, "args.source must be {type: huggingface, repo, file, revision?}"), nil
	}
	if src.Type != "huggingface" {
		return nil, errorf(protocol.ErrInvalidArgs, "args.source.type %q is not supported (only huggingface)", src.Type), nil
	}
	target := ""
	if _, present := args["target_name"]; present {
		if target, ok = argString(args, "target_name"); !ok {
			return nil, errorf(protocol.ErrInvalidArgs, "args.target_name must be a string"), nil
		}
	}
	if kind != "model" {
		return a.voiceDownload(ctx, voice.DownloadRequest{Kind: kind, Repo: src.Repo, File: src.File,
			Revision: src.Revision, TargetName: target})
	}
	hsrc := hf.Source{Repo: src.Repo, File: src.File, Revision: src.Revision}
	if err := hsrc.Validate(); err != nil {
		return nil, errorf(protocol.ErrInvalidArgs, "%v", err), nil
	}
	r, perr := a.runner()
	if perr != nil {
		return nil, perr, nil
	}
	req := runner.DownloadRequest{Repo: hsrc.Repo, File: hsrc.File, Revision: hsrc.Revision, TargetName: target}
	name := runner.DownloadName(req)
	if !modelNameRe.MatchString(name) {
		return nil, errorf(protocol.ErrInvalidArgs, "model name %q: use letters, digits, '.', '_' or '-'", name), nil
	}
	if models, err := r.ListModels(context.Background()); err == nil {
		for _, m := range models {
			if m.Name == name {
				return nil, errorf(protocol.ErrInvalidArgs, "model %s already exists on this node", name), nil
			}
		}
	}
	// Refuse at once when the file will not fit onto the model drive (D27). A check that
	// could not be made (Hugging Face unreachable …) never blocks: the download reports it.
	if c, ok := r.(runner.DownloadChecker); ok {
		cctx, cancel := context.WithTimeout(ctx, 15*time.Second)
		err := c.CheckDownload(cctx, req)
		cancel()
		if errors.Is(err, runner.ErrInsufficientDisk) {
			return nil, errorf(protocol.ErrInsufficientDisk, "%v", err), nil
		}
	}
	a.mu.Lock()
	if a.downloads[name] {
		a.mu.Unlock()
		return nil, errorf(protocol.ErrBusy, "model %s is already being downloaded", name), nil
	}
	a.downloads[name] = true
	a.mu.Unlock()
	return map[string]any{"started": true, "model": name}, nil, func() {
		a.bg.Add(1)
		go func() {
			defer a.bg.Done()
			a.download(r, req, name)
		}()
	}
}

func (a *Agent) download(r runner.ModelRunner, req runner.DownloadRequest, name string) {
	defer func() {
		a.mu.Lock()
		delete(a.downloads, name)
		a.mu.Unlock()
	}()
	ctx := a.ctx()
	progress := func(status string, done, total int64, err error) {
		d := map[string]any{"model": name, "status": status, "downloaded_bytes": done, "total_bytes": total}
		if err != nil {
			d["error"] = err.Error()
		}
		a.emit(protocol.EventDownloadProgress, d)
	}
	var lastDone, lastTotal int64
	req.Progress = hf.Throttle(func(done, total int64) {
		lastDone, lastTotal = done, total
		if done < total {
			progress("running", done, total, nil)
		}
	}, DownloadProgressInterval, a.opts.Now)
	a.log.Info("model download started", "model", name, "repo", req.Repo, "file", req.File)
	progress("running", 0, 0, nil)
	m, err := r.Download(ctx, req)
	if err != nil {
		a.log.Error("model download failed", "model", name, "err", err)
		progress("failed", lastDone, lastTotal, err)
		return
	}
	a.log.Info("model downloaded", "model", name, "bytes", m.SizeBytes)
	progress("done", m.SizeBytes, m.SizeBytes, nil)
	a.sendInventory(ctx)
}

// voiceDownload is models.download with target_kind stt or tts (#109): the file goes to
// <data dir>/voice-models/<kind>, not to the runner. Events and refusals are those of a model
// download; download_progress carries the kind.
func (a *Agent) voiceDownload(ctx context.Context, req voice.DownloadRequest) (any, *protocol.Error, func()) {
	d := a.opts.VoiceModels
	if d == nil {
		return nil, errorf(protocol.ErrNotImplemented, "this node cannot download voice models"), nil
	}
	name, err := d.Name(&req)
	if err != nil {
		return nil, errorf(protocol.ErrInvalidArgs, "%v", err), nil
	}
	cctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	err = d.Check(cctx, req, name)
	cancel()
	switch {
	case errors.Is(err, voice.ErrModelExists):
		return nil, errorf(protocol.ErrInvalidArgs, "%v", err), nil
	case errors.Is(err, voice.ErrInsufficientDisk):
		return nil, errorf(protocol.ErrInsufficientDisk, "%v", err), nil
	}
	key := req.Kind + ":" + name
	a.mu.Lock()
	if a.downloads[key] {
		a.mu.Unlock()
		return nil, errorf(protocol.ErrBusy, "%s model %s is already being downloaded", req.Kind, name), nil
	}
	a.downloads[key] = true
	a.mu.Unlock()
	return map[string]any{"started": true, "model": name, "kind": req.Kind}, nil, func() {
		a.bg.Add(1)
		go func() {
			defer a.bg.Done()
			a.downloadVoice(d, req, name, key)
		}()
	}
}

func (a *Agent) downloadVoice(d VoiceModelDownloader, req voice.DownloadRequest, name, key string) {
	defer func() {
		a.mu.Lock()
		delete(a.downloads, key)
		a.mu.Unlock()
	}()
	ctx := a.ctx()
	progress := func(status string, done, total int64, err error) {
		data := map[string]any{"model": name, "kind": req.Kind, "status": status, "downloaded_bytes": done, "total_bytes": total}
		if err != nil {
			data["error"] = err.Error()
		}
		a.emit(protocol.EventDownloadProgress, data)
	}
	var lastDone, lastTotal int64
	req.Progress = hf.Throttle(func(done, total int64) {
		lastDone, lastTotal = done, total
		if done < total {
			progress("running", done, total, nil)
		}
	}, DownloadProgressInterval, a.opts.Now)
	a.log.Info("voice model download started", "kind", req.Kind, "model", name, "repo", req.Repo, "file", req.File, "revision", req.Revision)
	progress("running", 0, 0, nil)
	size, err := d.Download(ctx, req, name)
	if err != nil {
		a.log.Error("voice model download failed", "kind", req.Kind, "model", name, "err", err)
		progress("failed", lastDone, lastTotal, err)
		return
	}
	a.log.Info("voice model downloaded", "kind", req.Kind, "model", name, "bytes", size)
	progress("done", size, size, nil)
	// the voice capabilities list their models in the inventory
	a.sendInventory(ctx)
}

func (a *Agent) actModelsDelete(ctx context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args, "model", "confirm"); perr != nil {
		return nil, perr, nil
	}
	model, ok := argString(args, "model")
	if !ok || model == "" {
		return nil, errorf(protocol.ErrInvalidArgs, "args.model must be a non-empty string"), nil
	}
	if c, ok := parseBool(args["confirm"]); !ok || !c {
		return nil, errorf(protocol.ErrInvalidArgs, "args.confirm must be true"), nil
	}
	r, perr := a.runner()
	if perr != nil {
		return nil, perr, nil
	}
	var wasLoaded *runner.LoadedModel
	if loaded, err := r.Loaded(ctx); err == nil {
		for _, l := range loaded {
			if l.Name == model {
				wasLoaded = &l
			}
		}
	}
	if err := r.Delete(ctx, model); err != nil {
		code := protocol.ErrRunnerError
		if errors.Is(err, runner.ErrUnknownModel) {
			code = protocol.ErrInvalidArgs
		}
		return nil, errorf(code, "delete %s: %v", model, err), nil
	}
	a.forget(model)
	return map[string]any{"deleted": model}, nil, func() {
		if wasLoaded != nil {
			a.emit(protocol.EventModelUnloaded, unloadedData(*wasLoaded, "deleted"))
			a.sendHeartbeat(ctx)
		}
		a.sendInventory(ctx)
	}
}

// benchRun is the benchmark in progress.
type benchRun struct {
	id     string
	cancel context.CancelCauseFunc
}

func (a *Agent) actNodeBenchmark(ctx context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args, "models", "quick"); perr != nil {
		return nil, perr, nil
	}
	var models []string
	if raw, ok := args["models"]; !ok || json.Unmarshal(raw, &models) != nil || len(models) == 0 || len(models) > maxBenchModels {
		return nil, errorf(protocol.ErrInvalidArgs, "args.models must be a list of 1..%d model names", maxBenchModels), nil
	}
	quick := false
	if raw, ok := args["quick"]; ok {
		if quick, ok = parseBool(raw); !ok {
			return nil, errorf(protocol.ErrInvalidArgs, "args.quick must be a boolean"), nil
		}
	}
	if a.isGaming() {
		return nil, errorf(protocol.ErrBusy, "node is busy gaming; no benchmark now"), nil
	}
	r, perr := a.runner()
	if perr != nil {
		return nil, perr, nil
	}
	b, ok := r.(runner.Benchmarkable)
	if !ok {
		return nil, errorf(protocol.ErrNotImplemented, "runner %s cannot benchmark", r.Type()), nil
	}
	target, err := b.BenchTarget(ctx)
	if err != nil {
		return nil, errorf(protocol.ErrRunnerError, "benchmark: %v", err), nil
	}
	seen := map[string]bool{}
	for _, m := range models {
		if seen[m] {
			return nil, errorf(protocol.ErrInvalidArgs, "model %s listed twice", m), nil
		}
		seen[m] = true
		if _, err := target.ModelPath(m); err != nil {
			return nil, errorf(protocol.ErrInvalidArgs, "%v", err), nil
		}
	}
	spec := bench.Spec{Models: models, Quick: quick}
	if info, err := a.opts.SysInfo.Info(ctx); err == nil {
		spec.Topology = bench.Topology{PerformanceCores: info.PerformanceCores, EfficiencyCores: info.EfficiencyCores, Threads: info.Threads}
	}
	if gpus, err := a.opts.GPU.GPUs(ctx); err == nil {
		for _, g := range gpus {
			spec.IntegratedGPU = spec.IntegratedGPU || g.Integrated
		}
	}

	a.mu.Lock()
	if a.bench != nil {
		a.mu.Unlock()
		return nil, errorf(protocol.ErrBusy, "benchmark %s is still running", a.bench.id), nil
	}
	bctx, cancel := context.WithCancelCause(a.baseCtx)
	// a whole benchmark that is not done after benchTimeout is ended (its results so far are kept)
	timer := time.AfterFunc(benchTimeout, func() {
		cancel(fmt.Errorf("timeout: the benchmark took longer than %s", benchTimeout))
	})
	run := &benchRun{id: protocol.NewUUID(), cancel: func(cause error) { timer.Stop(); cancel(cause) }}
	a.bench = run
	a.mu.Unlock()

	return map[string]any{"started": true, "benchmark_id": run.id}, nil, func() {
		a.emit(protocol.EventBenchmarkStarted, map[string]any{"benchmark_id": run.id, "models": models, "quick": quick})
		a.bg.Add(1)
		go func() {
			defer a.bg.Done()
			a.runBenchmark(bctx, run, target, spec)
		}()
	}
}

func (a *Agent) runBenchmark(ctx context.Context, run *benchRun, target runner.BenchTarget, spec bench.Spec) {
	a.log.Info("benchmark started", "id", run.id, "models", spec.Models, "quick", spec.Quick,
		"backend", target.Backend, "igpu", spec.IntegratedGPU)
	spec.OnFailure = func(f bench.RunError) {
		a.log.Warn("benchmark run failed; going on", "model", f.Model, "backend", f.Backend, "threads", f.Threads, "err", f.Err)
		a.emit(protocol.EventBenchmarkError, f.Data(run.id))
	}
	sum, err := bench.Run(ctx, target, spec, func(r bench.Result) {
		a.log.Info("benchmark result", "model", r.Model, "backend", r.Backend, "threads", r.Threads,
			"pp_tps", r.PromptTPS, "tg_tps", r.GenTPS)
		a.emit(protocol.EventBenchmarkResult, r.Data(run.id))
	})
	a.mu.Lock()
	if a.bench == run {
		a.bench = nil
	}
	a.mu.Unlock()
	data := map[string]any{"benchmark_id": run.id, "ok": err == nil}
	if n := len(sum.Failures); n > 0 {
		data["failed_runs"] = n
	}
	if err != nil {
		if cause := context.Cause(ctx); cause != nil && ctx.Err() != nil {
			err = cause
		}
		data["error"] = err.Error()
		a.log.Warn("benchmark failed", "id", run.id, "err", err)
	} else if sum.RecommendedThreads > 0 {
		data["recommended_threads"] = sum.RecommendedThreads
	}
	run.cancel(nil)
	a.emit(protocol.EventBenchmarkDone, data)
}

// abortBenchmark cancels a running benchmark (e.g. a game started).
func (a *Agent) abortBenchmark(reason string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.bench != nil {
		a.log.Warn("aborting benchmark", "id", a.bench.id, "reason", reason)
		a.bench.cancel(fmt.Errorf("aborted: %s", reason))
	}
}

// VoiceModelDownloader fetches voice models (voice.Downloader; a fake in tests).
type VoiceModelDownloader interface {
	// Name validates req, fills in defaults (the pinned Piper revision) and returns the model name.
	Name(req *voice.DownloadRequest) (string, error)
	// Check refuses with voice.ErrModelExists or voice.ErrInsufficientDisk (wrapped); any other
	// error means "could not check" and does not block.
	Check(ctx context.Context, req voice.DownloadRequest, name string) error
	// Download fetches the files and returns their size in bytes.
	Download(ctx context.Context, req voice.DownloadRequest, name string) (int64, error)
}
