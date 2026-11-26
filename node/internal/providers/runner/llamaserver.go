package runner

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"topcoat/node/internal/diskfree"
	"topcoat/node/internal/gguf"
	"topcoat/node/internal/hf"
	"topcoat/node/internal/llamacpp"
	"topcoat/node/internal/memfit"
	"topcoat/node/internal/proc"
	"topcoat/node/internal/providers/gpu"
	"topcoat/node/internal/providers/sysinfo"
)

// LlamaServerOptions configures the llama-server runner.
type LlamaServerOptions struct {
	// DataDir is the node data dir: models/ (downloads), runtime/ (llama.cpp
	// builds) and logs/ live below it.
	DataDir string
	// ModelDirs are extra read-only directories scanned for GGUF files.
	ModelDirs []string
	// Backend is the resolved backend (cuda, vulkan or cpu).
	Backend string
	// Release is the pinned llama.cpp release tag.
	Release string
	// Threads is the default -t for llama-server / llama-bench (0 = llama.cpp default).
	Threads int

	Installer *llamacpp.Installer
	HF        *hf.Client
	GPU       gpu.GPUProvider
	Logger    *slog.Logger

	// Injectable for tests; defaults are the real implementations.
	Start      proc.Starter
	Exec       ExecFunc
	HTTPClient *http.Client

	ReadyTimeout time.Duration // default 10 min (large models from a slow disk)
	StopTimeout  time.Duration // default 10 s
	// CancelWait is how long a canceled load waits for its killed server to be gone before it
	// returns (default 10 s); a server that lingers is waited for before the next one starts.
	CancelWait    time.Duration
	PollInterval  time.Duration // default 250 ms
	RetryInterval time.Duration // runtime install retry, default 1 min
	// DefaultContext is the context of one slot when a load does not ask for one (default 8192).
	DefaultContext int
	// MaxContext lets the runner grow the context per slot beyond a load's "at least" value
	// (LoadOptions.MinContext), up to this cap, as far as the memory holds. 0 = no growth: a load
	// gets what it asks for, the smallest context the work needs.
	MaxContext int
	// DefaultParallel is the number of slots (-np) when a load does not ask for a number
	// (default 1). llama-server's own default is "auto" = 4 slots sharing ONE window (verified
	// with b11223), so a long generation next to a chat could fill it: "Context size has been
	// exceeded".
	DefaultParallel int
	// ModelBackends overrides backend and GPU offload per model name (E2-05). Every backend
	// named here is installed next to the default one.
	ModelBackends map[string]ModelBackend
	// Projectors is runner.projectors: the vision projector per model name ("none" = none).
	Projectors map[string]string
	// SysInfo gives the free RAM for the memory guard (nil = no check).
	SysInfo sysinfo.Provider
	// GuardMode is refuse (default), warn or off; GuardReserveMB the RAM that must stay
	// free after a load (0 = automatic, see memfit.AutoReserveMB).
	GuardMode      string
	GuardReserveMB int64
	// DiskFree measures the drive of the model directory (default diskfree.Of);
	// DiskReserveMB stays free after a download (default 1024).
	DiskFree      diskfree.Func
	DiskReserveMB int64
	// FitMarginMB is the device memory --fit leaves free (0 = automatic: llama.cpp's default
	// with a discrete GPU, a quarter of the shared memory, at least 4 GB, with an iGPU).
	FitMarginMB int64
}

// LlamaServer supervises one llama-server process per loaded model
// (llama-swap style), each bound to 127.0.0.1 on a free port. It installs its
// pinned llama.cpp build in the background on creation.
//
// UNVERIFIED on real hardware: process start/health/stop against the official
// llama.cpp binaries, memory figures from the llama.cpp log. The logic is
// tested against a fake llama-server (see llamaserver_test.go).
type LlamaServer struct {
	opts LlamaServerOptions
	log  *slog.Logger

	mu       sync.Mutex
	insts    map[string]*llamacpp.Installation // by backend
	statuses map[string]string                 // human-readable install status while not ready
	notify   Notify
	loaded   map[string]*instance
	closed   bool
	cancelBg context.CancelFunc
	bgDone   chan struct{}

	frozen   atomic.Bool               // D24: model processes are suspended (also those starting)
	starting map[proc.Process]struct{} // started, not ready yet; guarded by mu
	dying    map[proc.Process]struct{} // killed, the system still tears them down; guarded by mu

	loadSem chan struct{} // loads are serialized (VRAM measurement, ports); a channel so a waiting load can be canceled
}

type instance struct {
	lm   LoadedModel
	proc proc.Process
	log  *ringBuffer
	file *os.File
	key  string // API key of this start (requests to the server must carry it)
	peak atomic.Int64
}

type modelFile struct {
	Name string
	Path string
	Size int64
	Own  bool // inside <data dir>/models (deletable)
	// Projector is the path of the vision projector loaded with the model (projector.go); "" = none.
	Projector string
}

// NewLlamaServer creates the runner and starts installing the runtime.
func NewLlamaServer(opts LlamaServerOptions) *LlamaServer {
	if opts.Logger == nil {
		opts.Logger = slog.Default()
	}
	if opts.Start == nil {
		opts.Start = proc.Start
	}
	if opts.Exec == nil {
		opts.Exec = execOutput
	}
	if opts.HTTPClient == nil {
		opts.HTTPClient = &http.Client{Timeout: 3 * time.Second}
	}
	if opts.HF == nil {
		opts.HF = &hf.Client{Token: os.Getenv("HF_TOKEN")}
	}
	if opts.ReadyTimeout <= 0 {
		opts.ReadyTimeout = 10 * time.Minute
	}
	if opts.CancelWait <= 0 {
		opts.CancelWait = 10 * time.Second
	}
	if opts.StopTimeout <= 0 {
		opts.StopTimeout = 10 * time.Second
	}
	if opts.PollInterval <= 0 {
		opts.PollInterval = 250 * time.Millisecond
	}
	if opts.RetryInterval <= 0 {
		opts.RetryInterval = time.Minute
	}
	if opts.DefaultParallel <= 0 {
		opts.DefaultParallel = 1
	}
	if opts.DefaultContext <= 0 {
		opts.DefaultContext = 8192
	}
	if opts.DiskFree == nil {
		opts.DiskFree = diskfree.Of
	}
	if opts.DiskReserveMB <= 0 {
		opts.DiskReserveMB = 1024
	}
	if opts.GuardMode == "" {
		opts.GuardMode = "refuse"
	}
	ctx, cancel := context.WithCancel(context.Background())
	l := &LlamaServer{
		opts:     opts,
		log:      opts.Logger.With("runner", "llamaserver", "backend", opts.Backend, "llamacpp", opts.Release),
		insts:    map[string]*llamacpp.Installation{},
		statuses: map[string]string{opts.Backend: "llama.cpp runtime not installed yet"},
		loaded:   map[string]*instance{},
		loadSem:  make(chan struct{}, 1),
		cancelBg: cancel,
		bgDone:   make(chan struct{}),
	}
	// The default backend, and every other backend a model is configured for.
	backends := []string{opts.Backend}
	for _, b := range l.extraBackends() {
		l.statuses[b] = "llama.cpp runtime not installed yet"
		backends = append(backends, b)
	}
	var wg sync.WaitGroup
	for _, b := range backends {
		wg.Add(1)
		go func(backend string) {
			defer wg.Done()
			l.installLoop(ctx, backend)
		}(b)
	}
	go func() {
		defer close(l.bgDone)
		wg.Wait()
	}()
	return l
}

// extraBackends lists the backends models are configured for besides the default one.
func (l *LlamaServer) extraBackends() []string {
	seen := map[string]bool{l.opts.Backend: true}
	var out []string
	// An iGPU-only node falls back to the CPU when a model does not fit the shared memory: the
	// CPU build has no GPU buffers at all, so it is installed next to the GPU build.
	if l.opts.Backend != llamacpp.BackendCPU {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_, only := l.integratedOnly(ctx)
		cancel()
		if only {
			seen[llamacpp.BackendCPU] = true
			out = append(out, llamacpp.BackendCPU)
		}
	}
	for _, mb := range l.opts.ModelBackends {
		if mb.Backend != "" && !seen[mb.Backend] {
			seen[mb.Backend] = true
			out = append(out, mb.Backend)
		}
	}
	sort.Strings(out)
	return out
}

// backendFor returns the backend and the fixed number of GPU layers (nil = fit) of a model.
func (l *LlamaServer) backendFor(model string) (backend string, gpuLayers *int) {
	backend = l.opts.Backend
	if mb, ok := l.opts.ModelBackends[model]; ok {
		if mb.Backend != "" {
			backend = mb.Backend
		}
		gpuLayers = mb.GPULayers
	}
	return backend, gpuLayers
}

func execOutput(ctx context.Context, bin string, args []string) ([]byte, error) {
	cmd := proc.CommandContext(ctx, bin, args...)
	cmd.Dir = filepath.Dir(bin)
	var stderr strings.Builder
	cmd.Stderr = &limitedWriter{w: &stderr, n: 8 << 10}
	out, err := cmd.Output()
	if err != nil {
		return out, fmt.Errorf("%w: %s", err, lastLines(stderr.String(), 5))
	}
	return out, nil
}

// installLoop installs the runtime of one backend, retrying after failures until it works
// or the runner is closed.
func (l *LlamaServer) installLoop(ctx context.Context, backend string) {
	for {
		if inst, ok := l.opts.Installer.Installed(l.opts.Release, backend); ok {
			l.setInstalled(backend, inst, false)
			return
		}
		l.setStatus(backend, fmt.Sprintf("installing llama.cpp %s (%s)", l.opts.Release, backend))
		l.log.Info("installing llama.cpp runtime", "backend", backend)
		var lastPct int64 = -1
		inst, err := l.opts.Installer.Ensure(ctx, l.opts.Release, backend, func(done, total int64) {
			if total <= 0 {
				return
			}
			pct := done * 100 / total
			if pct != lastPct {
				lastPct = pct
				l.setStatus(backend, fmt.Sprintf("installing llama.cpp %s (%s): %d%% of %d MB", l.opts.Release, backend, pct, total>>20))
			}
		})
		if err == nil {
			l.setInstalled(backend, inst, true)
			return
		}
		if ctx.Err() != nil {
			return
		}
		l.log.Error("llama.cpp runtime install failed; retrying", "backend", backend, "err", err, "retry_in", l.opts.RetryInterval)
		l.setStatus(backend, fmt.Sprintf("llama.cpp %s (%s) install failed: %v", l.opts.Release, backend, err))
		select {
		case <-ctx.Done():
			return
		case <-time.After(l.opts.RetryInterval):
		}
	}
}

func (l *LlamaServer) setStatus(backend, s string) {
	l.mu.Lock()
	l.statuses[backend] = s
	l.mu.Unlock()
}

func (l *LlamaServer) setInstalled(backend string, inst llamacpp.Installation, fresh bool) {
	l.mu.Lock()
	l.insts[backend] = &inst
	n := l.notify
	l.mu.Unlock()
	l.log.Info("llama.cpp runtime ready", "runtime_backend", backend, "dir", inst.Dir, "fresh", fresh)
	if fresh && n != nil {
		n("runtime_installed", map[string]any{"release": inst.Release, "backend": inst.Backend})
	}
}

// installation is the default backend's runtime (what Health reports on).
func (l *LlamaServer) installation() (llamacpp.Installation, error) {
	return l.installationFor(l.opts.Backend)
}

func (l *LlamaServer) installationFor(backend string) (llamacpp.Installation, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.closed {
		return llamacpp.Installation{}, errors.New("runner closed")
	}
	inst, ok := l.insts[backend]
	if !ok {
		if st := l.statuses[backend]; st != "" {
			return llamacpp.Installation{}, errors.New(st)
		}
		return llamacpp.Installation{}, fmt.Errorf("llama.cpp runtime for backend %q is not installed", backend)
	}
	return *inst, nil
}

// SetNotify implements Notifier.
func (l *LlamaServer) SetNotify(n Notify) {
	l.mu.Lock()
	l.notify = n
	l.mu.Unlock()
}

// RuntimeInfo implements Describer.
func (l *LlamaServer) RuntimeInfo() RuntimeInfo {
	return RuntimeInfo{Backend: l.opts.Backend, Release: l.opts.Release, Threads: l.opts.Threads}
}

// Type implements ModelRunner.
func (l *LlamaServer) Type() string { return "llamaserver" }

// Health implements ModelRunner: available once the runtime is installed.
func (l *LlamaServer) Health(context.Context) error {
	_, err := l.installation()
	return err
}

// Close implements Closer: stops the install and every llama-server process.
func (l *LlamaServer) Close(ctx context.Context) {
	l.mu.Lock()
	l.closed = true
	l.mu.Unlock()
	l.cancelBg()
	<-l.bgDone
	if _, err := l.unloadAll(ctx); err != nil {
		l.log.Warn("stop llama-server processes", "err", err)
	}
}

// --- model store --------------------------------------------------------------

func (l *LlamaServer) ownDir() string { return filepath.Join(l.opts.DataDir, "models") }

var (
	splitPartRe  = regexp.MustCompile(`-(\d{5})-of-\d{5}$`)
	modelNameRe  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
	maxScanDepth = 4
)

// scan lists GGUF models: the node's own models/ first, then model_dirs.
func (l *LlamaServer) scan() []modelFile {
	seen := map[string]bool{}
	var out []modelFile
	projectors := map[string][]string{} // directory -> projector file names in it
	subfolder := map[string]bool{}      // directory -> below a scanned root
	add := func(dir string, own bool) {
		root := filepath.Clean(dir)
		_ = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
			if err != nil {
				if d != nil && d.IsDir() && p != root {
					return filepath.SkipDir
				}
				return nil
			}
			if d.IsDir() {
				if rel, _ := filepath.Rel(root, p); rel != "." && strings.Count(rel, string(filepath.Separator))+1 > maxScanDepth {
					return filepath.SkipDir
				}
				return nil
			}
			if isProjector(d.Name()) {
				pdir := filepath.Dir(p)
				projectors[pdir] = append(projectors[pdir], d.Name())
				subfolder[pdir] = pdir != root
				return nil
			}
			name, ok := modelName(d.Name())
			if !ok || seen[name] {
				return nil
			}
			info, err := d.Info()
			if err != nil {
				return nil
			}
			seen[name] = true
			out = append(out, modelFile{Name: name, Path: p, Size: info.Size(), Own: own})
			return nil
		})
	}
	add(l.ownDir(), true)
	for _, d := range l.opts.ModelDirs {
		if strings.TrimSpace(d) != "" {
			add(d, false)
		}
	}
	for i := range out {
		dir := filepath.Dir(out[i].Path)
		out[i].Projector = projectorFor(out[i].Name, dir, projectors[dir], l.opts.Projectors[out[i].Name], subfolder[dir])
	}
	return out
}

// modelName maps a file name to a model name; ok is false for files that are
// not loadable models (not GGUF, multimodal projectors, later split parts).
func modelName(file string) (string, bool) {
	if !strings.EqualFold(filepath.Ext(file), ".gguf") {
		return "", false
	}
	base := file[:len(file)-len(".gguf")]
	if strings.Contains(strings.ToLower(base), "mmproj") {
		return "", false
	}
	if m := splitPartRe.FindStringSubmatch(base); m != nil {
		if m[1] != "00001" {
			return "", false
		}
		base = base[:len(base)-len(m[0])]
	}
	return base, base != ""
}

func (l *LlamaServer) find(name string) (modelFile, error) {
	for _, m := range l.scan() {
		if m.Name == name {
			return m, nil
		}
	}
	return modelFile{}, fmt.Errorf("%w: %s", ErrUnknownModel, name)
}

// ListModels implements ModelRunner.
func (l *LlamaServer) ListModels(context.Context) ([]Model, error) {
	files := l.scan()
	out := make([]Model, 0, len(files))
	for _, f := range files {
		out = append(out, Model{Name: f.Name, SizeBytes: f.Size, Format: "gguf", Own: f.Own, GGUF: headerOf(f.Path), Projector: baseOrEmpty(f.Projector)})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

// ModelFile implements ModelFiler.
func (l *LlamaServer) ModelFile(name string) (string, int64, bool, error) {
	m, err := l.find(name)
	if err != nil {
		return "", 0, false, err
	}
	base := strings.TrimSuffix(filepath.Base(m.Path), filepath.Ext(m.Path))
	if splitPartRe.MatchString(base) {
		return "", 0, false, fmt.Errorf("%w: %s", ErrSplitModel, name)
	}
	return m.Path, m.Size, m.Own, nil
}

// ReceiveDir implements ModelReceiver.
func (l *LlamaServer) ReceiveDir() string { return l.ownDir() }

// ModelDir implements DiskReporter.
func (l *LlamaServer) ModelDir() string { return l.ownDir() }

// CheckDownload implements DownloadChecker: the size comes from the Hugging Face metadata,
// what a resumable .part file already holds is not counted again.
func (l *LlamaServer) CheckDownload(ctx context.Context, req DownloadRequest) error {
	src := hf.Source{Repo: req.Repo, File: req.File, Revision: req.Revision}
	if err := src.Validate(); err != nil {
		return err
	}
	info, err := l.opts.HF.Info(ctx, src)
	if err != nil {
		return err
	}
	free, err := l.opts.DiskFree(l.ownDir())
	if err != nil {
		return err
	}
	var have int64
	if st, err := os.Stat(filepath.Join(l.ownDir(), DownloadName(req)+".gguf.part")); err == nil {
		have = st.Size()
	}
	need := max(info.Size-have, 0)
	reserve := l.opts.DiskReserveMB << 20
	if free.FreeBytes < need+reserve {
		return fmt.Errorf("%w: %s needs %d MB, %d MB must stay free, but only %d MB are free on the drive of %s",
			ErrInsufficientDisk, req.File, need>>20, l.opts.DiskReserveMB, free.FreeBytes>>20, l.ownDir())
	}
	return nil
}

// Download implements ModelRunner (Hugging Face, resumable, SHA-256 verified).
func (l *LlamaServer) Download(ctx context.Context, req DownloadRequest) (Model, error) {
	src := hf.Source{Repo: req.Repo, File: req.File, Revision: req.Revision}
	if err := src.Validate(); err != nil {
		return Model{}, err
	}
	name := DownloadName(req)
	if !modelNameRe.MatchString(name) {
		return Model{}, fmt.Errorf("model name %q: use letters, digits, '.', '_' or '-'", name)
	}
	if _, err := l.find(name); err == nil {
		return Model{}, fmt.Errorf("%w: %s", ErrModelExists, name)
	}
	if err := l.CheckDownload(ctx, req); errors.Is(err, ErrInsufficientDisk) {
		return Model{}, err
	}
	if err := os.MkdirAll(l.ownDir(), 0o750); err != nil {
		return Model{}, err
	}
	dest := filepath.Join(l.ownDir(), name+".gguf")
	info, err := l.opts.HF.Download(ctx, src, dest, req.Progress)
	if err != nil {
		return Model{}, err
	}
	return Model{Name: name, SizeBytes: info.Size, Format: "gguf", Own: true}, nil
}

// DownloadName is the model name a download will get: target_name, or the
// file name without directory and .gguf.
func DownloadName(req DownloadRequest) string {
	name := strings.TrimSpace(req.TargetName)
	if name == "" {
		name = filepath.Base(filepath.FromSlash(req.File))
	}
	if strings.EqualFold(filepath.Ext(name), ".gguf") {
		name = name[:len(name)-len(".gguf")]
	}
	return name
}

// Delete implements ModelRunner. Only files the node downloaded itself are
// deleted, never files in the configured model_dirs.
func (l *LlamaServer) Delete(ctx context.Context, name string) error {
	m, err := l.find(name)
	if err != nil {
		return err
	}
	if !m.Own {
		return fmt.Errorf("%s is in a configured model directory (%s); the node only deletes its own downloads", name, filepath.Dir(m.Path))
	}
	if err := l.Unload(ctx, name); err != nil && !errors.Is(err, ErrNotLoaded) {
		return err
	}
	return os.Remove(m.Path)
}

// --- processes ----------------------------------------------------------------

// Loaded implements ModelRunner.
func (l *LlamaServer) Loaded(context.Context) ([]LoadedModel, error) {
	l.mu.Lock()
	insts := make([]*instance, 0, len(l.loaded))
	for _, in := range l.loaded {
		insts = append(insts, in)
	}
	l.mu.Unlock()
	out := make([]LoadedModel, 0, len(insts))
	for _, in := range insts {
		lm := in.lm
		// The working set of a process shrinks and grows as the OS trims it (33 … 5177 MB
		// for the same 7B); the highest value seen is what the model needs.
		lm.RAMUsedMB = in.peakRAM(in.proc.RSSMB())
		out = append(out, lm)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

// peakRAM records a sample and returns the highest one seen since the model was loaded.
func (in *instance) peakRAM(sample int64) int64 {
	for {
		old := in.peak.Load()
		if sample <= old {
			return old
		}
		if in.peak.CompareAndSwap(old, sample) {
			return sample
		}
	}
}

// Freeze implements Freezer: every model process (also one that is still starting) is
// suspended where it is. The wait for a starting server does not run out meanwhile (waitReady).
func (l *LlamaServer) Freeze(context.Context) error {
	l.frozen.Store(true)
	var errs []error
	for _, p := range l.processes() {
		if err := p.Suspend(); err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

// Thaw implements Freezer.
func (l *LlamaServer) Thaw(context.Context) error {
	l.frozen.Store(false)
	var errs []error
	for _, p := range l.processes() {
		if err := p.Resume(); err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

// processes lists the loaded and the starting model processes.
func (l *LlamaServer) processes() []proc.Process {
	l.mu.Lock()
	defer l.mu.Unlock()
	ps := make([]proc.Process, 0, len(l.loaded)+len(l.starting))
	for _, in := range l.loaded {
		ps = append(ps, in.proc)
	}
	for p := range l.starting {
		ps = append(ps, p)
	}
	return ps
}

// Load implements ModelRunner.
//
// A load on a GPU backend without a configured layer count is planned by loadPlans and tried
// at most twice: on an integrated GPU (shared memory) the whole model on the GPU if it fits
// into the shared memory with a safety margin, else the CPU straight away — never a partial
// offload, whose host buffers come out of the same shared budget and always failed; on a discrete
// GPU llama.cpp's fit with the margin, half of the layers, then the CPU (D27). Each retry is
// reported through the notifier as `model_load_retry`.
//
// Before every attempt the memory guard looks at the free RAM again and the server of the
// failed attempt is gone: a killed process may take seconds to give its memory back.
func (l *LlamaServer) Load(ctx context.Context, name string, opts LoadOptions) (LoadedModel, error) {
	backend, gpuLayers := l.backendFor(name)
	inst, err := l.installationFor(backend)
	if err != nil {
		return LoadedModel{}, err
	}
	m, err := l.find(name)
	if err != nil {
		return LoadedModel{}, err
	}
	// Loads are serialized (VRAM measurement, ports); a waiting load can be canceled.
	select {
	case l.loadSem <- struct{}{}:
		defer func() { <-l.loadSem }()
	case <-ctx.Done():
		return LoadedModel{}, ctx.Err()
	}
	l.mu.Lock()
	if in, ok := l.loaded[name]; ok {
		l.mu.Unlock()
		return in.lm, nil
	}
	l.mu.Unlock()

	slots := opts.NParallel
	if slots <= 0 {
		slots = l.opts.DefaultParallel
	}
	ctxLen := opts.ContextLength // per slot
	if ctxLen <= 0 {
		ctxLen = max(l.opts.DefaultContext, opts.MinContext)
		if opts.MinContext > 0 {
			ctxLen = l.autoContext(ctx, m, name, opts.MinContext, slots, backend, gpuLayers)
		}
	}
	jinja := !opts.NoJinja
	var lastErr error
	var freeBefore int64
	for i, plan := range l.loadPlans(ctx, m, name, ctxLen*slots, slots, backend, gpuLayers) {
		if err := l.waitGone(ctx); err != nil { // an earlier server may still be dying
			return LoadedModel{}, err
		}
		if i > 0 {
			l.settleMemory(ctx, freeBefore)
			l.log.Warn("load failed for lack of memory; trying again", "model", name, "attempt", i+1, "plan", plan.label, "err", loadFailure(lastErr))
			l.emit("model_load_retry", map[string]any{"model": name, "attempt": i + 1, "plan": plan.label, "error": loadFailure(lastErr)})
		}
		if err := l.checkMemory(ctx, m, name, ctxLen*slots, slots, plan); err != nil { // the KV cache is per slot
			if lastErr != nil {
				return LoadedModel{}, fmt.Errorf("%w (after the attempt before failed: %s)", err, loadFailure(lastErr))
			}
			return LoadedModel{}, err
		}
		freeBefore = l.freeRAMMB(ctx)
		pinst := inst
		if plan.cpu && inst.Backend != llamacpp.BackendCPU {
			// no device at all: the CPU build, where it is installed, has no GPU buffers either
			if ci, err := l.installationFor(llamacpp.BackendCPU); err == nil {
				pinst = ci
			}
		}
		lm, err := l.launch(ctx, pinst, m, name, ctxLen, slots, jinja, plan)
		if err == nil {
			return lm, nil
		}
		lastErr = err
		if ctx.Err() != nil || errors.Is(err, errNotStopped) || !isOutOfMemory(err.Error()) {
			break
		}
	}
	return LoadedModel{}, lastErr
}

// errNotStopped marks a failed server that could not be ended: nothing else is started then.
var errNotStopped = errors.New("the failed llama-server could not be stopped")

// freeRAMMB is the free system memory in MB (0 = not measurable).
func (l *LlamaServer) freeRAMMB(ctx context.Context) int64 {
	if l.opts.SysInfo == nil {
		return 0
	}
	info, err := l.opts.SysInfo.Info(ctx)
	if err != nil || info.RAMTotalMB <= 0 {
		return 0
	}
	return info.RAMTotalMB - info.RAMUsedMB
}

// settleMemory waits (at most 10 s) until the memory of the failed attempt is free again: the
// free RAM is back to within 1 GB of what it was before that attempt started.
func (l *LlamaServer) settleMemory(ctx context.Context, before int64) {
	if before <= 0 {
		return
	}
	deadline := time.Now().Add(10 * time.Second)
	for {
		free := l.freeRAMMB(ctx)
		if free == 0 || free >= before-1024 || time.Now().After(deadline) || ctx.Err() != nil {
			l.log.Info("memory after the failed attempt", "free_mb_before", before, "free_mb_now", free)
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
}

// abort ends a starting server whose load was canceled: killed at once (a server busy loading
// does not react to a polite request), and waited for up to CancelWait. One that lingers is
// remembered; the next start waits for it.
func (l *LlamaServer) abort(p proc.Process) {
	_ = p.Kill()
	select {
	case <-p.Done():
	case <-time.After(l.opts.CancelWait):
		l.markDying(p)
	}
}

func (l *LlamaServer) markDying(p proc.Process) {
	l.mu.Lock()
	if l.dying == nil {
		l.dying = map[proc.Process]struct{}{}
	}
	l.dying[p] = struct{}{}
	l.mu.Unlock()
	go func() {
		<-p.Done()
		l.mu.Lock()
		delete(l.dying, p)
		l.mu.Unlock()
	}()
}

// waitGone waits until no killed server is still being torn down (its memory is not free yet).
func (l *LlamaServer) waitGone(ctx context.Context) error {
	deadline := time.Now().Add(2 * time.Minute)
	for {
		l.mu.Lock()
		n := len(l.dying)
		l.mu.Unlock()
		if n == 0 {
			return nil
		}
		if time.Now().After(deadline) {
			return errors.New("a llama-server that was killed is still shutting down; try again in a moment")
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(100 * time.Millisecond):
		}
	}
}

// loadPlan is one way to start a model: which backend build's device use, how many layers.
type loadPlan struct {
	label     string
	gpuLayers *int
	all       bool  // every layer on the device, no fit (-ngl all --fit off)
	cpu       bool  // --device none
	noHost    bool  // --no-host: no pinned host buffers of the GPU backend
	nCPUMoe   *int  // --n-cpu-moe: MoE experts of the first N layers stay in RAM
	fitMargin int64 // MiB left free on the device by --fit (0 = llama.cpp's default)
}

// loadPlans lists the attempts for a model. Where retrying makes no sense (the CPU backend, a
// fixed number of layers) there is one. On an integrated GPU: all layers if the model fits the
// shared memory, else the CPU. On a discrete GPU: fit -> half of the layers -> CPU.
func (l *LlamaServer) loadPlans(ctx context.Context, m modelFile, name string, ctxLen, slots int, backend string, gpuLayers *int) []loadPlan {
	noHost := l.opts.ModelBackends[name].NoHost
	if backend == llamacpp.BackendCPU {
		return []loadPlan{{label: "cpu", cpu: true}}
	}
	if moe := l.opts.ModelBackends[name].NCPUMoe; moe != nil {
		// one explicit placement: experts in RAM, the rest on the GPU; no fit, no fallback
		// (UNVERIFIED on real hardware)
		return []loadPlan{{label: "expert offload", gpuLayers: gpuLayers, all: gpuLayers == nil, noHost: noHost, nCPUMoe: moe}}
	}
	if gpuLayers != nil {
		return []loadPlan{{label: "fixed layers", gpuLayers: gpuLayers, noHost: noHost}}
	}
	if gpus, ok := l.integratedOnly(ctx); ok {
		if budget, known := l.sharedBudgetMB(ctx, gpus); known {
			meta, _ := gguf.ReadFile(m.Path)
			need := memfit.Estimate(memfit.Model{FileBytes: m.Size, Meta: meta, Context: ctxLen, Slots: slots, GPU: true, Integrated: true}).SystemMB
			if need <= budget {
				return []loadPlan{{label: "all layers", all: true, noHost: noHost}, {label: "cpu", cpu: true}}
			}
			l.log.Info("the model does not fit the iGPU's shared memory; loading it on the CPU",
				"model", name, "need_mb", need, "budget_mb", budget)
			return []loadPlan{{label: "cpu", cpu: true}}
		}
	}
	plans := []loadPlan{{label: "fit", fitMargin: l.fitMargin(ctx, backend), noHost: noHost}}
	if meta, err := gguf.ReadFile(m.Path); err == nil && meta.BlockCount > 1 {
		half := meta.BlockCount / 2
		plans = append(plans, loadPlan{label: fmt.Sprintf("%d of %d layers", half, meta.BlockCount+1), gpuLayers: &half, noHost: noHost})
	}
	return append(plans, loadPlan{label: "cpu", cpu: true})
}

// integratedOnly returns the GPUs when the machine has only integrated ones (shared memory).
func (l *LlamaServer) integratedOnly(ctx context.Context) ([]gpu.GPU, bool) {
	if l.opts.GPU == nil {
		return nil, false
	}
	gpus, _ := l.opts.GPU.GPUs(ctx)
	if len(gpus) == 0 {
		return nil, false
	}
	for _, g := range gpus {
		if !g.Integrated {
			return nil, false
		}
	}
	return gpus, true
}

// sharedBudgetMB is the shared memory a new model may take on an integrated GPU: what the GPU
// can address, minus the safety margin (a quarter, at least 4 GB) that the user's programs and
// the operating system need, minus what models already on the GPU hold. Its host buffers come
// out of the same budget, which is why a partial offload never worked there. Unknown (false)
// if neither the GPU nor the system says how much there is.
func (l *LlamaServer) sharedBudgetMB(ctx context.Context, gpus []gpu.GPU) (int64, bool) {
	var total int64
	for _, g := range gpus {
		total = max(total, g.SharedTotalMB)
	}
	if total <= 0 && l.opts.SysInfo != nil {
		if info, err := l.opts.SysInfo.Info(ctx); err == nil {
			total = info.RAMTotalMB / 2 // the default: an iGPU may address half of the RAM
		}
	}
	if total <= 0 {
		return 0, false
	}
	l.mu.Lock()
	var held int64
	for _, in := range l.loaded {
		if in.lm.Backend != llamacpp.BackendCPU {
			held += in.lm.VRAMUsedMB
		}
	}
	l.mu.Unlock()
	margin := IntegratedFitMargin(gpus)
	if l.opts.FitMarginMB > 0 {
		margin = l.opts.FitMarginMB
	}
	return total - margin - held, true
}

// FitMargin is the memory --fit leaves free on the device, in MiB: what the config says, else
// 0 (llama.cpp's default, 1024) with a discrete GPU. An iGPU's "free memory" is system memory
// the driver reports as free although the operating system and the user's programs need
// it (seen on Windows: 15 GB "free" on a 32 GB laptop, the load then failed after 30 s), so
// it gets a quarter of its shared memory, at least 4 GB.
func (l *LlamaServer) fitMargin(ctx context.Context, backend string) int64 {
	if l.opts.FitMarginMB > 0 {
		return l.opts.FitMarginMB
	}
	if backend == llamacpp.BackendCPU || l.opts.GPU == nil {
		return 0
	}
	gpus, _ := l.opts.GPU.GPUs(ctx)
	return IntegratedFitMargin(gpus)
}

// IntegratedFitMargin is the fit margin for an integrated GPU (0 if there is none).
func IntegratedFitMargin(gpus []gpu.GPU) int64 {
	var margin int64
	for _, g := range gpus {
		if g.Integrated {
			margin = max(margin, 4096, g.SharedTotalMB/4)
		}
	}
	return margin
}

func (l *LlamaServer) emit(kind string, data map[string]any) {
	l.mu.Lock()
	n := l.notify
	l.mu.Unlock()
	if n != nil {
		n(kind, data)
	}
}

// launch starts one llama-server for a plan and waits until it answers.
func (l *LlamaServer) launch(ctx context.Context, inst llamacpp.Installation, m modelFile, name string, ctxLen, slots int, jinja bool, plan loadPlan) (LoadedModel, error) {
	before := l.gpuUsed(ctx)
	port, err := freePort()
	if err != nil {
		return LoadedModel{}, err
	}
	key, keyFile, err := l.newAPIKey(port)
	if err != nil {
		return LoadedModel{}, err
	}
	defer removeFile(keyFile)        // read at start; the server keeps the key in memory
	meta, _ := gguf.ReadFile(m.Path) // unreadable: served as a chat model, as before
	args := l.serverArgs(m.Path, name, port, ctxLen, slots, jinja, meta.IsEmbedding(), plan, keyFile)
	if m.Projector != "" && !meta.IsEmbedding() {
		// UNVERIFIED on real hardware: a vision model with its projector (D125); the projector's
		// memory is not in the fit estimate, --fit on leaves room for it on the GPU
		args = append(args, "--mmproj", m.Projector)
	}
	ring := &ringBuffer{max: 64 << 10}
	var out io.Writer = ring
	logFile := l.openLog(name)
	if logFile != nil {
		out = io.MultiWriter(ring, &limitedWriter{w: logFile, n: maxModelLogBytes})
	}
	device := inst.Backend
	if plan.cpu {
		device = "none" // --device none: the CPU build or not, no device is used
	}
	l.log.Info("starting llama-server", "model", name, "port", port, "ctx", ctxLen, "slots", slots, "device", device, "plan", plan.label, "fit_margin_mb", plan.fitMargin)
	p, err := l.opts.Start(ctx, inst.Server, args, inst.Dir, out)
	if err != nil {
		closeFile(logFile)
		return LoadedModel{}, fmt.Errorf("start llama-server: %w", err)
	}
	l.mu.Lock()
	if l.starting == nil {
		l.starting = map[proc.Process]struct{}{}
	}
	l.starting[p] = struct{}{}
	l.mu.Unlock()
	if l.frozen.Load() {
		_ = p.Suspend() // started while the node ducks: it starts frozen
	}
	err = l.waitReady(ctx, p, port)
	l.mu.Lock()
	delete(l.starting, p)
	l.mu.Unlock()
	if err != nil {
		switch {
		case ctx.Err() != nil:
			l.abort(p) // canceled: no polite request, the server is killed
		case p.Stop(l.opts.StopTimeout) != nil:
			l.markDying(p)
			closeFile(logFile)
			return LoadedModel{}, fmt.Errorf("%w: %w\n%s", errNotStopped, err, lastLines(ring.String(), 8))
		}
		closeFile(logFile)
		return LoadedModel{}, fmt.Errorf("%w\n%s", err, lastLines(ring.String(), 8))
	}
	logText := ring.String()
	vram := deviceBuffersMB(logText)
	if vram == 0 {
		vram = usedDelta(before, l.gpuUsed(ctx))
	}
	if n := parseNCtxSeq(logText); n > 0 {
		ctxLen = n // what one slot really got (llama.cpp clamps it to what the model was trained for)
	} else if n := parseNCtx(logText); n > 0 {
		ctxLen = n / slots
	}
	backend := inst.Backend
	if plan.cpu {
		backend = llamacpp.BackendCPU // no device in use, whatever build runs it
	}
	in := &instance{
		lm: LoadedModel{
			Name: name, VRAMUsedMB: vram, ContextLength: ctxLen, NParallel: slots,
			PID: p.PID(), Port: port, Backend: backend, RAMUsedMB: p.RSSMB(),
		},
		proc: p, log: ring, file: logFile, key: key,
	}
	l.mu.Lock()
	l.loaded[name] = in
	l.mu.Unlock()
	if l.frozen.Load() {
		_ = p.Suspend() // Freeze may have run in the moment between ready and registered
	}
	go l.watchExit(name, in)
	l.log.Info("model loaded", "model", name, "pid", in.lm.PID, "port", port, "vram_mb", vram, "ctx", ctxLen, "slots", slots, "plan", plan.label)
	return in.lm, nil
}

// outOfMemory matches what llama.cpp and its backends print when an allocation fails.
var outOfMemory = regexp.MustCompile(`(?i)failed to allocate|out of memory|outofdevicememory|outofhostmemory|cudamalloc failed|unable to allocate|cannot allocate memory|not enough memory`)

func isOutOfMemory(text string) bool { return outOfMemory.MatchString(text) }

// newAPIKey creates the key of one llama-server start: 32 random bytes, handed to the process
// through a file only its owner can read (a command line is visible to every local user).
func (l *LlamaServer) newAPIKey(port int) (key, file string, err error) {
	var raw [32]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", "", err
	}
	key = hex.EncodeToString(raw[:])
	dir := filepath.Join(l.opts.DataDir, "run")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", "", err
	}
	file = filepath.Join(dir, fmt.Sprintf("llama-server-%d.key", port))
	if err := os.WriteFile(file, []byte(key+"\n"), 0o600); err != nil {
		return "", "", err
	}
	return key, file, nil
}

func removeFile(path string) {
	if path != "" {
		_ = os.Remove(path)
	}
}

// loadFailure names why a start failed: the log line about the failed allocation if there is
// one, else the first line of the error.
func loadFailure(err error) string {
	for _, line := range strings.Split(err.Error(), "\n") {
		if isOutOfMemory(line) {
			return firstLine(line)
		}
	}
	return firstLine(err.Error())
}

// firstLine keeps the first non-empty line of an error (a failed start appends the server log).
func firstLine(s string) string {
	for _, l := range strings.Split(s, "\n") {
		if l = strings.TrimSpace(l); l != "" {
			if len(l) > 300 {
				l = l[:300]
			}
			return l
		}
	}
	return s
}

// serverArgs builds the llama-server command line. On a GPU backend llama.cpp's own fit
// (--fit, on by default since b7xxx and verified for the pinned release with --help) puts as
// many layers on the device as its memory holds, leaving `plan.fitMargin` MiB free; a fixed
// number of layers is passed only when the config or a retry names one.
//
// The server listens on the loopback only and answers only requests that carry the key of
// this start (`--api-key-file`; /health stays public); CORS allows the localhost origins
// and no credentials, so a web page in the user's browser cannot talk to it. Verified with the
// pinned b11223 (401 without the key, no Access-Control-Allow-Origin for a foreign origin).
//
// UNVERIFIED on real hardware: --fit on a Vulkan iGPU with shared memory (llama.cpp's
// device-memory report decides how many layers it offloads); the flags themselves were
// verified with `llama-server --help` and a CPU run of the pinned b11223.
func (l *LlamaServer) serverArgs(path, name string, port, ctxLen, slots int, jinja, embedding bool, plan loadPlan, keyFile string) []string {
	args := []string{
		"-m", path,
		"--host", "127.0.0.1",
		"--port", strconv.Itoa(port),
		"--alias", name,
		"--no-webui",
		"--api-key-file", keyFile,
		"--cors-origins", "localhost",
		"--no-cors-credentials",
		// ctxLen is the context of ONE request. With an explicit -np llama-server gives every slot
		// an equal share of -c and keeps the windows apart (kv_unified off; verified with b11223:
		// -c 1024 -np 2 -> n_ctx_seq 512), so -c is context x slots. Without -np the server
		// starts 4 slots in one shared window, where one long story starves the others.
		"-np", strconv.Itoa(slots),
		"-c", strconv.Itoa(ctxLen * slots),
		// Verbosity 4 (trace) prints the per-device buffer sizes the memory
		// figure is computed from (verified with b11223).
		"-lv", "4",
	}
	// The model's own chat template engine: the prerequisite of native tool calls (OpenAI tools /
	// tool_calls). b11223 has it on by default (--help); it is passed explicitly so a pinned
	// release that does not keeps working. The built-in agent tools of llama-server (--tools,
	// --agent: shell, files) are never enabled.
	switch {
	case embedding:
		// An embedding model has no chat template and is not run as a chat model: started like
		// one, llama.cpp stops at the warm-up ("embeddings required but some input tokens were
		// not marked as outputs"). --embeddings serves /v1/embeddings; the pooling comes from the
		// file. Its attention looks at the whole input at once, so a batch must hold a whole
		// request (-b/-ub = the context of one request), or longer texts are refused.
		// UNVERIFIED on real hardware (bge-m3 on the pinned b11223).
		args = append(args, "--embeddings", "-b", strconv.Itoa(ctxLen), "-ub", strconv.Itoa(ctxLen))
	case jinja:
		args = append(args, "--jinja")
	default:
		args = append(args, "--no-jinja")
	}
	if l.opts.Threads > 0 {
		args = append(args, "-t", strconv.Itoa(l.opts.Threads))
	}
	switch {
	case plan.cpu:
		args = append(args, "--device", "none")
	case plan.all:
		args = append(args, "-ngl", "all", "--fit", "off")
	case plan.gpuLayers != nil:
		args = append(args, "-ngl", strconv.Itoa(*plan.gpuLayers))
	default:
		args = append(args, "--fit", "on")
		if plan.fitMargin > 0 {
			args = append(args, "--fit-target", strconv.FormatInt(plan.fitMargin, 10))
		}
	}
	if plan.nCPUMoe != nil {
		args = append(args, "--n-cpu-moe", strconv.Itoa(*plan.nCPUMoe))
	}
	if plan.noHost {
		// UNVERIFIED on real hardware: keeps the weights of layers that stay on the CPU out of the
		// GPU backend's pinned host buffer (the "Vulkan_Host" buffer of an iGPU's shared memory)
		args = append(args, "--no-host")
	}
	return args
}

// autoContext sizes the context of a load that asked for "at least min". By default that is the
// floor itself: the smallest context the work needs, because a smaller context is faster. Only a
// node with a cap (MaxContext) grows it, to the largest the machine holds judged for the plan the
// load will try first (memfit.AutoContext). Without a measurement of the free memory there is
// nothing to size by, and the floor is used.
//
// UNVERIFIED on real hardware: the growth is tested (memfit) and the wiring against the fake
// runner, but the sizes it picks have not been compared with the real VRAM use of llama-server on
// the AMD (Vulkan) and CPU machines yet.
func (l *LlamaServer) autoContext(ctx context.Context, m modelFile, name string, min, slots int, backend string, gpuLayers *int) int {
	floor := max(l.opts.DefaultContext, min)
	if l.opts.MaxContext <= 0 {
		return floor
	}
	in, ok := l.fitInput(ctx, m, floor, slots, backend, gpuLayers)
	if !ok {
		return floor
	}
	in.Min = floor
	in.Max = l.opts.MaxContext
	r := memfit.AutoContext(in)
	l.log.Info("context sized by the node", "model", name, "min", min, "ctx", r.Context, "on_device", r.OnDevice, "why", r.Reason)
	return r.Context
}

// fitInput collects what memfit.AutoContext needs for the plan a load tries first; false when the
// free memory cannot be measured.
func (l *LlamaServer) fitInput(ctx context.Context, m modelFile, floor, slots int, backend string, gpuLayers *int) (memfit.AutoInput, bool) {
	if l.opts.SysInfo == nil {
		return memfit.AutoInput{}, false
	}
	info, err := l.opts.SysInfo.Info(ctx)
	if err != nil || info.RAMTotalMB <= 0 {
		return memfit.AutoInput{}, false
	}
	plans := l.loadPlans(ctx, m, m.Name, floor*slots, slots, backend, gpuLayers)
	if len(plans) == 0 {
		return memfit.AutoInput{}, false
	}
	plan := plans[0]
	meta, _ := gguf.ReadFile(m.Path)
	mm := memfit.Model{FileBytes: m.Size, Meta: meta, Slots: slots, GPU: !plan.cpu && (plan.gpuLayers == nil || *plan.gpuLayers > 0)}
	if mm.GPU && l.opts.GPU != nil {
		gpus, _ := l.opts.GPU.GPUs(ctx)
		for _, g := range gpus {
			if g.Integrated {
				mm.Integrated = true
			} else if free := g.VRAMTotalMB - g.VRAMUsedMB; free > mm.FreeVRAMMB {
				mm.FreeVRAMMB = free
			}
		}
		if mm.Integrated && mm.FreeVRAMMB > 0 {
			mm.Integrated = false // a discrete GPU exists too: the model goes there first
		}
	}
	reserve := l.opts.GuardReserveMB
	if reserve <= 0 {
		reserve = memfit.AutoReserveMB(info.RAMTotalMB)
	}
	margin := plan.fitMargin
	if margin <= 0 {
		margin = 1024 // llama.cpp's own default for --fit-target
	}
	return memfit.AutoInput{Model: mm, FreeRAMMB: info.RAMTotalMB - info.RAMUsedMB, ReserveMB: reserve, VRAMMarginMB: margin}, true
}

// checkMemory refuses (or warns about) a load that would not fit into the free RAM.
//
// UNVERIFIED on real hardware: the estimate is checked against the llama.cpp log of a tiny
// model (KV cache exact) and against figures from the maintainer's machines (overhead), not
// against a real 30B load; the free-RAM figure on Windows is "available" physical memory.
func (l *LlamaServer) checkMemory(ctx context.Context, m modelFile, name string, ctxLen, slots int, plan loadPlan) error {
	if l.opts.SysInfo == nil || l.opts.GuardMode == "off" {
		return nil
	}
	info, err := l.opts.SysInfo.Info(ctx)
	if err != nil || info.RAMTotalMB <= 0 {
		return nil // no measurement: no check
	}
	meta, _ := gguf.ReadFile(m.Path) // unreadable metadata: the KV cache is guessed
	mm := memfit.Model{FileBytes: m.Size, Meta: meta, Context: ctxLen, Slots: slots, GPU: !plan.cpu && (plan.gpuLayers == nil || *plan.gpuLayers > 0)}
	if mm.GPU && l.opts.GPU != nil {
		gpus, _ := l.opts.GPU.GPUs(ctx)
		for _, g := range gpus {
			if g.Integrated {
				mm.Integrated = true
			} else if free := g.VRAMTotalMB - g.VRAMUsedMB; free > mm.FreeVRAMMB {
				mm.FreeVRAMMB = free
			}
		}
		if mm.Integrated && mm.FreeVRAMMB == 0 {
			// only an iGPU: its memory is system memory
		} else if mm.Integrated {
			mm.Integrated = false // a discrete GPU exists too: the model goes there first
		}
	}
	reserve := l.opts.GuardReserveMB
	if reserve <= 0 {
		reserve = memfit.AutoReserveMB(info.RAMTotalMB)
	}
	v := memfit.Check(mm, info.RAMTotalMB-info.RAMUsedMB, reserve)
	if v.OK {
		return nil
	}
	l.log.Warn("not enough free RAM for a load", "model", name, "need_mb", v.Need.SystemMB, "free_mb", v.FreeMB, "reserve_mb", v.ReserveMB, "mode", l.opts.GuardMode)
	if l.opts.GuardMode == "warn" {
		l.mu.Lock()
		n := l.notify
		l.mu.Unlock()
		if n != nil {
			n("error", map[string]any{"code": "insufficient_memory", "model": name, "message": v.Message()})
		}
		return nil
	}
	return fmt.Errorf("%w: %s", ErrInsufficientMemory, v.Message())
}

func (l *LlamaServer) openLog(name string) *os.File {
	dir := filepath.Join(l.opts.DataDir, "logs")
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil
	}
	safe := strings.Map(func(r rune) rune {
		if r == '.' || r == '-' || r == '_' || (r >= '0' && r <= '9') || (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') {
			return r
		}
		return '_'
	}, name)
	f, err := os.Create(filepath.Join(dir, "llama-server-"+safe+".log"))
	if err != nil {
		return nil
	}
	return f
}

func closeFile(f *os.File) {
	if f != nil {
		_ = f.Close()
	}
}

func (l *LlamaServer) waitReady(ctx context.Context, p proc.Process, port int) error {
	deadline := time.NewTimer(l.opts.ReadyTimeout)
	defer deadline.Stop()
	tick := time.NewTicker(l.opts.PollInterval)
	defer tick.Stop()
	url := "http://127.0.0.1:" + strconv.Itoa(port) + "/health"
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-p.Done():
			return fmt.Errorf("llama-server exited during load: %w", p.Err())
		case <-deadline.C:
			return fmt.Errorf("llama-server not ready after %s", l.opts.ReadyTimeout)
		case <-tick.C:
			if l.frozen.Load() {
				// The node ducks for its user (D24): a frozen server cannot become ready, and the
				// time it stands still does not count against it.
				if !deadline.Stop() {
					select {
					case <-deadline.C:
					default:
					}
				}
				deadline.Reset(l.opts.ReadyTimeout)
				continue
			}
			req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
			resp, err := l.opts.HTTPClient.Do(req)
			if err != nil {
				continue // not listening yet
			}
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return nil
			}
		}
	}
}

// watchExit removes a model whose process ended on its own and reports it.
func (l *LlamaServer) watchExit(name string, in *instance) {
	<-in.proc.Done()
	closeFile(in.file)
	l.mu.Lock()
	current, ok := l.loaded[name]
	crashed := ok && current == in
	if crashed {
		delete(l.loaded, name)
	}
	n := l.notify
	l.mu.Unlock()
	if !crashed {
		return
	}
	msg := lastLines(in.log.String(), 3)
	l.log.Error("llama-server exited unexpectedly", "model", name, "err", in.proc.Err(), "log", msg)
	if n != nil {
		data := map[string]any{"model": name, "reason": "exited", "error": fmt.Sprintf("%v: %s", in.proc.Err(), msg)}
		if in.lm.VRAMUsedMB > 0 {
			data["vram_used_mb"] = in.lm.VRAMUsedMB
		}
		n("model_unloaded", data)
	}
}

// Unload implements ModelRunner.
func (l *LlamaServer) Unload(_ context.Context, name string) error {
	l.mu.Lock()
	in, ok := l.loaded[name]
	if ok {
		delete(l.loaded, name)
	}
	l.mu.Unlock()
	if !ok {
		return fmt.Errorf("%w: %s", ErrNotLoaded, name)
	}
	l.log.Info("stopping llama-server", "model", name, "pid", in.lm.PID)
	return in.proc.Stop(l.opts.StopTimeout)
}

// UnloadAll implements ModelRunner.
func (l *LlamaServer) UnloadAll(ctx context.Context) ([]LoadedModel, error) {
	return l.unloadAll(ctx)
}

func (l *LlamaServer) unloadAll(ctx context.Context) ([]LoadedModel, error) {
	loaded, _ := l.Loaded(ctx)
	var out []LoadedModel
	var errs []error
	var wg sync.WaitGroup
	var mu sync.Mutex
	for _, lm := range loaded {
		wg.Add(1)
		go func(lm LoadedModel) {
			defer wg.Done()
			err := l.Unload(ctx, lm.Name)
			mu.Lock()
			defer mu.Unlock()
			if err != nil && !errors.Is(err, ErrNotLoaded) {
				errs = append(errs, err)
				return
			}
			out = append(out, lm)
		}(lm)
	}
	wg.Wait()
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, errors.Join(errs...)
}

// BenchTarget implements Benchmarkable.
func (l *LlamaServer) BenchTarget(ctx context.Context) (BenchTarget, error) {
	inst, err := l.installation()
	if err != nil {
		return BenchTarget{}, err
	}
	return BenchTarget{
		Bench: inst.Bench, Backend: l.opts.Backend, Release: l.opts.Release, Threads: l.opts.Threads,
		For: func(model string) (ModelBench, error) {
			backend, layers := l.backendFor(model)
			moe := l.opts.ModelBackends[model].NCPUMoe
			in, err := l.installationFor(backend)
			if err != nil {
				return ModelBench{}, err
			}
			// the benchmark leaves the same memory free as a load does (the server's fit margin)
			return ModelBench{Bench: in.Bench, Backend: backend, GPULayers: layers, FitMarginMB: l.fitMargin(ctx, backend), NCPUMoe: moe}, nil
		},
		ModelPath: func(name string) (string, error) {
			m, err := l.find(name)
			return m.Path, err
		},
		Exec: l.opts.Exec,
	}, nil
}

// --- measurements -----------------------------------------------------------

// gpuUsed returns used memory (dedicated + shared) per GPU index.
func (l *LlamaServer) gpuUsed(ctx context.Context) map[int]int64 {
	out := map[int]int64{}
	if l.opts.GPU == nil {
		return out
	}
	gpus, _ := l.opts.GPU.GPUs(ctx)
	for _, g := range gpus {
		out[g.Index] = g.VRAMUsedMB + g.SharedUsedMB
	}
	return out
}

func usedDelta(before, after map[int]int64) int64 {
	var sum int64
	for i, a := range after {
		if d := a - before[i]; d > 0 {
			sum += d
		}
	}
	return sum
}

// Device buffer lines in the llama.cpp log, e.g.
//
//	load_tensors:        CUDA0 model buffer size =  4403.49 MiB
//	llama_kv_cache:    Vulkan0 KV buffer size =   448.00 MiB
//	sched_reserve:      CUDA0 compute buffer size =   304.00 MiB
//
// Host buffers (CPU, CUDA_Host, Vulkan_Host) are RAM and not counted.
var (
	deviceBufRe = regexp.MustCompile(`\b(?:CUDA|Vulkan|ROCm|SYCL)\d+\s+[A-Za-z_]+ buffer size\s*=\s*([0-9.]+)\s*MiB`)
	nCtxRe      = regexp.MustCompile(`\bn_ctx\s*=\s*(\d+)`)
	nCtxSeqRe   = regexp.MustCompile(`\bn_ctx_seq\s*=\s*(\d+)`)
)

func deviceBuffersMB(log string) int64 {
	var sum float64
	for _, m := range deviceBufRe.FindAllStringSubmatch(log, -1) {
		v, err := strconv.ParseFloat(m[1], 64)
		if err == nil {
			sum += v
		}
	}
	return int64(sum + 0.5)
}

func parseNCtx(log string) int {
	m := nCtxRe.FindStringSubmatch(log)
	if m == nil {
		return 0
	}
	n, _ := strconv.Atoi(m[1])
	return n
}

// parseNCtxSeq is the context of one slot the llama_context line reports.
func parseNCtxSeq(log string) int {
	m := nCtxSeqRe.FindStringSubmatch(log)
	if m == nil {
		return 0
	}
	n, _ := strconv.Atoi(m[1])
	return n
}

func freePort() (int, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port, nil
}

// maxModelLogBytes caps logs/llama-server-<model>.log (rewritten per load).
const maxModelLogBytes = 16 << 20

// ringBuffer keeps the last max bytes written.
type ringBuffer struct {
	mu  sync.Mutex
	buf []byte
	max int
}

func (r *ringBuffer) Write(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.buf = append(r.buf, p...)
	if over := len(r.buf) - r.max; over > 0 {
		r.buf = append([]byte(nil), r.buf[over:]...)
	}
	return len(p), nil
}

func (r *ringBuffer) String() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return string(r.buf)
}

type limitedWriter struct {
	w io.Writer
	n int
}

func (l *limitedWriter) Write(p []byte) (int, error) {
	if l.n > 0 {
		k := min(len(p), l.n)
		_, _ = l.w.Write(p[:k])
		l.n -= k
	}
	return len(p), nil
}

func lastLines(s string, n int) string {
	lines := strings.Split(strings.TrimRight(s, "\n"), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

// loopbackClient talks to the llama-server processes: never through a proxy
// from the environment, no overall timeout (responses stream for minutes).
var loopbackClient = &http.Client{Transport: &http.Transport{
	Proxy:                 nil,
	MaxIdleConnsPerHost:   4,
	IdleConnTimeout:       90 * time.Second,
	ResponseHeaderTimeout: 10 * time.Minute, // prompt processing of a long context
}}

// ProxyTarget implements Proxier: the loopback port of the model's llama-server.
//
// UNVERIFIED on real hardware (the gateway is tested against the fake runner).
func (l *LlamaServer) ProxyTarget(_ context.Context, name string) (ProxyTarget, error) {
	l.mu.Lock()
	in, ok := l.loaded[name]
	l.mu.Unlock()
	if !ok || in.lm.Port <= 0 {
		return ProxyTarget{}, fmt.Errorf("%w: %s", ErrNotLoaded, name)
	}
	return ProxyTarget{BaseURL: "http://127.0.0.1:" + strconv.Itoa(in.lm.Port), Client: loopbackClient, APIKey: in.key}, nil
}

// headerCache keeps the header facts of a model file until the file changes: the inventory is
// built often and a vocabulary of a few MB must not be read every time.
var headerCache = struct {
	sync.Mutex
	m map[string]cachedHeader
}{m: map[string]cachedHeader{}}

type cachedHeader struct {
	size int64
	mod  time.Time
	meta *gguf.Meta
}

// headerOf reads the header of a model file once per size and modification time; nil when the
// file cannot be read as GGUF (the hub then keeps judging by the name).
func headerOf(path string) *gguf.Meta {
	st, err := os.Stat(path)
	if err != nil {
		return nil
	}
	headerCache.Lock()
	defer headerCache.Unlock()
	if c, ok := headerCache.m[path]; ok && c.size == st.Size() && c.mod.Equal(st.ModTime()) {
		return c.meta
	}
	meta, err := gguf.ReadFile(path)
	var out *gguf.Meta
	if err == nil || meta.Architecture != "" { // a file without layer counts still names its kind
		out = &meta
	}
	headerCache.m[path] = cachedHeader{size: st.Size(), mod: st.ModTime(), meta: out}
	return out
}
