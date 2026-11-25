// Package runner defines the ModelRunner interface and the real runner stubs.
package runner

import (
	"context"
	"errors"
	"net/http"
	"time"

	"topcoat/node/internal/gguf"
)

// Model is a model available to the runner (on disk / in its catalog).
type Model struct {
	Name      string
	SizeBytes int64
	Format    string
	// Own is true when the file lives in the node's own <data dir>/models (the hub may move or
	// delete it), false for a configured model_dirs entry (read-only for the hub).
	Own bool
	// GGUF is what the file states in its header (nil = not read or not a GGUF file).
	GGUF *gguf.Meta
}

// LoadedModel is a model currently loaded.
type LoadedModel struct {
	Name       string
	VRAMUsedMB int64
	// ContextLength is the context of ONE slot (what a single request may use); the server's
	// whole KV window is ContextLength x NParallel.
	ContextLength int
	// NParallel is the number of slots (llama-server -np); 0 is read as 1.
	NParallel int
	PID       int
	Port      int
	Backend   string
	RAMUsedMB int64
}

// LoadOptions are optional load parameters (zero = runner default).
type LoadOptions struct {
	// ContextLength is the context of one slot.
	ContextLength int
	// MinContext asks for "at least this context per slot, as much more as fits" when
	// ContextLength is 0: the runner sizes the context itself (memfit.AutoContext).
	MinContext int
	// NParallel is the number of slots (0 = the runner's default, 1).
	NParallel int
	// NoJinja starts llama-server with --no-jinja (default: --jinja, native tool calls work).
	NoJinja bool
	TTL     time.Duration
}

// ModelBackend overrides the backend and the GPU offload for one model (E2-05).
type ModelBackend struct {
	// Backend is cuda, vulkan or cpu; empty = the runner's backend.
	Backend string
	// GPULayers is the exact number of layers on the GPU (0 = none); nil = fit to memory.
	GPULayers *int
	// NoHost passes --no-host (no pinned host buffers of the GPU backend); UNVERIFIED on real
	// hardware, an experiment for a partial offload on an iGPU.
	NoHost bool
	// NCPUMoe is llama-server --n-cpu-moe (MoE experts of the first N layers stay in RAM); nil = off.
	NCPUMoe *int
}

// DownloadRequest describes a model download from Hugging Face.
type DownloadRequest struct {
	Repo       string
	File       string
	Revision   string
	TargetName string
	// Progress is called with downloaded / total bytes (may be nil).
	Progress func(done, total int64)
}

// ModelRunner lists/downloads/loads/unloads models.
type ModelRunner interface {
	// Type is the runner type as used in the protocol (llamaserver, fake).
	Type() string
	// Health returns nil if the runner is reachable and usable.
	Health(ctx context.Context) error
	ListModels(ctx context.Context) ([]Model, error)
	Loaded(ctx context.Context) ([]LoadedModel, error)
	Load(ctx context.Context, name string, opts LoadOptions) (LoadedModel, error)
	Unload(ctx context.Context, name string) error
	// UnloadAll unloads every loaded model and returns the names that were unloaded.
	UnloadAll(ctx context.Context) ([]LoadedModel, error)
	// Download blocks until the model is stored and verified.
	Download(ctx context.Context, req DownloadRequest) (Model, error)
	Delete(ctx context.Context, name string) error
}

// RuntimeInfo describes the inference runtime for the inventory.
type RuntimeInfo struct {
	Backend string
	Release string
	Threads int
}

// Describer is implemented by runners that report runtime details.
type Describer interface {
	RuntimeInfo() RuntimeInfo
}

// Notify reports asynchronous runner events (protocol event kind + data).
type Notify func(kind string, data map[string]any)

// Notifier is implemented by runners that report asynchronous events, e.g. a
// llama-server process that exited on its own or a finished runtime install.
type Notifier interface {
	SetNotify(Notify)
}

// Freezer is implemented by runners whose model processes can be suspended and continued at
// the same place (D24 step 2): while frozen a model neither computes nor answers, and nothing it
// holds in memory is lost. Both calls are idempotent. A process that starts while frozen starts
// frozen.
type Freezer interface {
	Freeze(ctx context.Context) error
	Thaw(ctx context.Context) error
}

// Closer is implemented by runners that own processes; Close stops them.
type Closer interface {
	Close(ctx context.Context)
}

// ExecFunc runs a short-lived program and returns its stdout.
type ExecFunc func(ctx context.Context, bin string, args []string) ([]byte, error)

// BenchTarget is what the benchmark needs from a runner.
type BenchTarget struct {
	// Bench is the llama-bench binary of the pinned runtime.
	Bench   string
	Backend string
	Release string
	Threads int
	// For resolves the binary, backend and fixed GPU layers of one model when models use
	// different backends (config.runner.model_backends); nil = the fields above for all.
	For func(model string) (ModelBench, error)
	// ModelPath resolves a model name to its GGUF file.
	ModelPath func(name string) (string, error)
	Exec      ExecFunc
}

// ModelBench is how one model is benchmarked.
type ModelBench struct {
	Bench   string
	Backend string
	// GPULayers is the fixed number of GPU layers, nil = let llama.cpp fit to device memory.
	GPULayers *int
	// FitMarginMB is the device memory a fit run leaves free, the same value a load uses (an iGPU
	// gets a large one, see LlamaServer.fitMargin); 0 = llama.cpp's default.
	FitMarginMB int64
	// NCPUMoe is the number of layers whose MoE experts stay in RAM (llama-bench -ncmoe); nil = off.
	NCPUMoe *int
}

// Benchmarkable is implemented by runners that can run llama-bench.
type Benchmarkable interface {
	BenchTarget(ctx context.Context) (BenchTarget, error)
}

// ProxyTarget is where HTTP requests for a loaded model go: the loopback
// llama-server of that model, or an in-process handler for the fake runner.
type ProxyTarget struct {
	BaseURL string
	Client  *http.Client
	// APIKey is the key of this llama-server start; the node adds it as a bearer token, so
	// only the node (never a program or web page on the same machine) can talk to the server.
	APIKey string
}

// Proxier is implemented by runners whose loaded models answer OpenAI-style
// HTTP requests (E1-05). Target fails with ErrNotLoaded if the model is not loaded.
type Proxier interface {
	ProxyTarget(ctx context.Context, name string) (ProxyTarget, error)
}

// DiskReporter is implemented by runners that keep models in a directory of their own.
type DiskReporter interface {
	// ModelDir is where downloaded models are stored (it may not exist yet).
	ModelDir() string
}

// DownloadChecker is implemented by runners that can refuse a download up front, before
// the action answers: CheckDownload returns ErrInsufficientDisk (wrapped) if the file does
// not fit onto the drive of the model directory. Other errors mean "could not check" and
// never block a download.
type DownloadChecker interface {
	CheckDownload(ctx context.Context, req DownloadRequest) error
}

// ModelFiler is implemented by runners whose models are files the node can send to another
// node (E2-07). ModelFile resolves a model name inside the runner's own directories, never a
// path from the wire; own is false for a file in a configured model_dirs entry (it can be
// sent, never deleted). Split GGUFs fail with ErrSplitModel, unknown names with ErrUnknownModel.
type ModelFiler interface {
	ModelFile(name string) (path string, size int64, own bool, err error)
}

// ModelReceiver is implemented by runners that can take a model file from another node:
// ReceiveDir is the directory the file <name>.gguf is placed in (it may not exist yet).
type ModelReceiver interface {
	ReceiveDir() string
}

// ErrSplitModel is returned (wrapped) for a model that consists of several GGUF files.
var ErrSplitModel = errors.New("split GGUF models are not supported")

// ErrModelExists is returned when a download target already exists.
var ErrModelExists = errors.New("model already exists")

// ErrUnknownModel is returned when a model name is not known to the runner.
var ErrUnknownModel = errors.New("unknown model")

// ErrInsufficientMemory is returned (wrapped) when a load would not fit into the free RAM.
var ErrInsufficientMemory = errors.New("insufficient memory")

// ErrInsufficientDisk is returned (wrapped) when a download would not fit onto the drive.
var ErrInsufficientDisk = errors.New("insufficient disk space")

// ErrNotLoaded is returned when unloading a model that is not loaded.
var ErrNotLoaded = errors.New("model not loaded")

// Unavailable is a runner that is never available, e.g. when the configured type
// cannot be used on this node. All operations fail with Reason.
type Unavailable struct {
	RunnerType string
	Reason     error
}

func (u Unavailable) err() error {
	if u.Reason == nil {
		return errors.New("runner unavailable")
	}
	return u.Reason
}

// Type implements ModelRunner.
func (u Unavailable) Type() string { return u.RunnerType }

// Health implements ModelRunner.
func (u Unavailable) Health(context.Context) error { return u.err() }

// ListModels implements ModelRunner.
func (u Unavailable) ListModels(context.Context) ([]Model, error) { return nil, u.err() }

// Loaded implements ModelRunner.
func (u Unavailable) Loaded(context.Context) ([]LoadedModel, error) { return nil, u.err() }

// Load implements ModelRunner.
func (u Unavailable) Load(context.Context, string, LoadOptions) (LoadedModel, error) {
	return LoadedModel{}, u.err()
}

// Unload implements ModelRunner.
func (u Unavailable) Unload(context.Context, string) error { return u.err() }

// UnloadAll implements ModelRunner.
func (u Unavailable) UnloadAll(context.Context) ([]LoadedModel, error) { return nil, u.err() }

// Download implements ModelRunner.
func (u Unavailable) Download(context.Context, DownloadRequest) (Model, error) {
	return Model{}, u.err()
}

// Delete implements ModelRunner.
func (u Unavailable) Delete(context.Context, string) error { return u.err() }

// None is the runner of a node without models (role tools, E2-04): it is always healthy, lists
// nothing and refuses every model operation. The node still has a state, a heartbeat and
// capabilities; it just never downloads llama.cpp or touches a GPU.
type None struct{}

// errNoRunner is what a model operation on a node without a runner says.
var errNoRunner = errors.New("this node has no model runner (role tools)")

// Type implements ModelRunner.
func (None) Type() string { return "none" }

// Health implements ModelRunner.
func (None) Health(context.Context) error { return nil }

// ListModels implements ModelRunner.
func (None) ListModels(context.Context) ([]Model, error) { return nil, nil }

// Loaded implements ModelRunner.
func (None) Loaded(context.Context) ([]LoadedModel, error) { return nil, nil }

// Load implements ModelRunner.
func (None) Load(context.Context, string, LoadOptions) (LoadedModel, error) {
	return LoadedModel{}, errNoRunner
}

// Unload implements ModelRunner.
func (None) Unload(context.Context, string) error { return errNoRunner }

// UnloadAll implements ModelRunner.
func (None) UnloadAll(context.Context) ([]LoadedModel, error) { return nil, nil }

// Download implements ModelRunner.
func (None) Download(context.Context, DownloadRequest) (Model, error) {
	return Model{}, errNoRunner
}

// Delete implements ModelRunner.
func (None) Delete(context.Context, string) error { return errNoRunner }
