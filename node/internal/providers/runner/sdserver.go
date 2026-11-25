package runner

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"topcoat/node/internal/llamacpp"
	"topcoat/node/internal/proc"
)

// The image runner (D126): stable-diffusion.cpp's sd-server next to llama-server. Like
// llama-server it is a pinned official build (sdcpp/releases.json, verified by SHA-256), started
// per model, bound to 127.0.0.1 on a free port and reached only through the node. One image model
// is loaded at a time (image models are large and run one job after the other anyway).
//
// sd-server has no API key (checked: the release pinned here and master of 2026-11-25 have no
// option for one and no authentication in the server code), so unlike llama-server it is
// protected only by the loopback address and the random port: any program of the same machine
// could reach it while it runs. The node unloads it when the work is done.

// ImageModel is an image model file the runner can load.
type ImageModel struct {
	Name      string
	SizeBytes int64
	Format    string // gguf or safetensors
}

// LoadedImageModel is a running sd-server.
type LoadedImageModel struct {
	Name string
	PID  int
	Port int
}

// ImageRunner manages the image server (D126). Implementations: SDServer, and fakes in tests.
type ImageRunner interface {
	// Info says which build the runner uses and whether it is installed.
	Info() ImageRuntimeInfo
	ListImageModels(ctx context.Context) ([]ImageModel, error)
	LoadImage(ctx context.Context, name string) (LoadedImageModel, error)
	LoadedImages(ctx context.Context) ([]LoadedImageModel, error)
	UnloadImage(ctx context.Context, name string) error
	Close(ctx context.Context)
}

// ImageRuntimeInfo is what the inventory reports about the image runner.
type ImageRuntimeInfo struct {
	Release   string
	Backend   string
	Installed bool
}

// imageExts are the files sd.cpp loads that the node offers. Pickle-based .ckpt/.pt files are
// left out on purpose: loading one can run code.
var imageExts = map[string]string{".gguf": "gguf", ".safetensors": "safetensors"}

// SDServerOptions configure the image runner.
type SDServerOptions struct {
	// DataDir is the node data dir: image-models/ and runtime-sd/ live below it.
	DataDir string
	// ModelDirs are extra read-only directories scanned for image models.
	ModelDirs []string
	Backend   string
	Release   string
	Threads   int
	Installer *llamacpp.Installer
	Logger    *slog.Logger

	// Injectable for tests.
	Start        proc.Starter
	HTTPClient   *http.Client
	ReadyTimeout time.Duration // default 10 min
	StopTimeout  time.Duration // default 10 s
	PollInterval time.Duration // default 250 ms
}

// SDServer is the stable-diffusion.cpp image runner.
type SDServer struct {
	opts SDServerOptions
	log  *slog.Logger

	mu      sync.Mutex
	running *sdInstance
}

type sdInstance struct {
	lm   LoadedImageModel
	proc proc.Process
}

// NewSDServer creates the image runner. Nothing is downloaded or started until a model loads.
func NewSDServer(opts SDServerOptions) *SDServer {
	if opts.Start == nil {
		opts.Start = proc.Start
	}
	if opts.HTTPClient == nil {
		// loopback only: never through a proxy from the environment
		opts.HTTPClient = &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{Proxy: nil}}
	}
	if opts.ReadyTimeout <= 0 {
		opts.ReadyTimeout = 10 * time.Minute
	}
	if opts.StopTimeout <= 0 {
		opts.StopTimeout = 10 * time.Second
	}
	if opts.PollInterval <= 0 {
		opts.PollInterval = 250 * time.Millisecond
	}
	log := opts.Logger
	if log == nil {
		log = slog.Default()
	}
	return &SDServer{opts: opts, log: log.With("runner", "sdserver", "backend", opts.Backend, "release", opts.Release)}
}

func (s *SDServer) modelDir() string { return filepath.Join(s.opts.DataDir, "image-models") }

// Info implements ImageRunner.
func (s *SDServer) Info() ImageRuntimeInfo {
	_, ok := s.opts.Installer.Installed(s.opts.Release, s.opts.Backend)
	return ImageRuntimeInfo{Release: s.opts.Release, Backend: s.opts.Backend, Installed: ok}
}

// ListImageModels implements ImageRunner: the node's own image-models/ first, then ModelDirs.
func (s *SDServer) ListImageModels(context.Context) ([]ImageModel, error) {
	files := s.scan()
	out := make([]ImageModel, 0, len(files))
	for _, f := range files {
		out = append(out, f.model)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

type imageFile struct {
	model ImageModel
	path  string
}

func (s *SDServer) scan() []imageFile {
	seen := map[string]bool{}
	var out []imageFile
	add := func(dir string) {
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
			format, ok := imageExts[strings.ToLower(filepath.Ext(d.Name()))]
			if !ok {
				return nil
			}
			name := strings.TrimSuffix(d.Name(), filepath.Ext(d.Name()))
			if !modelNameRe.MatchString(name) || seen[name] {
				return nil
			}
			info, err := d.Info()
			if err != nil {
				return nil
			}
			seen[name] = true
			out = append(out, imageFile{model: ImageModel{Name: name, SizeBytes: info.Size(), Format: format}, path: p})
			return nil
		})
	}
	add(s.modelDir())
	for _, d := range s.opts.ModelDirs {
		if strings.TrimSpace(d) != "" {
			add(d)
		}
	}
	return out
}

// LoadImage implements ImageRunner. Loading another model stops the running one first.
func (s *SDServer) LoadImage(ctx context.Context, name string) (LoadedImageModel, error) {
	var file imageFile
	found := false
	for _, f := range s.scan() {
		if f.model.Name == name {
			file, found = f, true
			break
		}
	}
	if !found {
		return LoadedImageModel{}, fmt.Errorf("%w: %s", ErrUnknownModel, name)
	}
	s.mu.Lock()
	if s.running != nil && s.running.lm.Name == name {
		lm := s.running.lm
		s.mu.Unlock()
		return lm, nil
	}
	old := s.running
	s.running = nil
	s.mu.Unlock()
	if old != nil {
		s.log.Info("stopping sd-server for another model", "model", old.lm.Name)
		_ = old.proc.Stop(s.opts.StopTimeout)
	}

	inst, err := s.opts.Installer.Ensure(ctx, s.opts.Release, s.opts.Backend, nil)
	if err != nil {
		return LoadedImageModel{}, fmt.Errorf("install stable-diffusion.cpp %s/%s: %w", s.opts.Release, s.opts.Backend, err)
	}
	port, err := freePort()
	if err != nil {
		return LoadedImageModel{}, err
	}
	args := sdServerArgs(file.path, port, s.opts.Threads)
	ring := &ringBuffer{max: 64 << 10}
	s.log.Info("starting sd-server", "model", name, "port", port)
	// UNVERIFIED on real hardware: sd-server of the pinned release on the nodes' GPUs (Vulkan on
	// Windows/AMD, CUDA 12 on Windows/NVIDIA); the Linux builds need glibc 2.43.
	p, err := s.opts.Start(ctx, inst.Server, args, inst.Dir, io.Writer(ring))
	if err != nil {
		return LoadedImageModel{}, fmt.Errorf("start sd-server: %w", err)
	}
	if err := s.waitReady(ctx, p, port); err != nil {
		_ = p.Kill()
		if tail := strings.TrimSpace(ring.String()); tail != "" {
			err = fmt.Errorf("%w: %s", err, lastLines(tail, 3))
		}
		return LoadedImageModel{}, err
	}
	lm := LoadedImageModel{Name: name, PID: p.PID(), Port: port}
	s.mu.Lock()
	s.running = &sdInstance{lm: lm, proc: p}
	s.mu.Unlock()
	go s.watchExit(name, p)
	s.log.Info("image model loaded", "model", name, "pid", lm.PID, "port", port)
	return lm, nil
}

// sdServerArgs are the arguments of one sd-server: loopback only, no web page of its own.
func sdServerArgs(model string, port, threads int) []string {
	args := []string{
		"--listen-ip", "127.0.0.1",
		"--listen-port", strconv.Itoa(port),
		"-m", model,
	}
	if threads > 0 {
		args = append(args, "-t", strconv.Itoa(threads))
	}
	return args
}

func (s *SDServer) waitReady(ctx context.Context, p proc.Process, port int) error {
	deadline := time.NewTimer(s.opts.ReadyTimeout)
	defer deadline.Stop()
	tick := time.NewTicker(s.opts.PollInterval)
	defer tick.Stop()
	url := "http://127.0.0.1:" + strconv.Itoa(port) + "/v1/models"
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-p.Done():
			return fmt.Errorf("sd-server exited during load: %w", p.Err())
		case <-deadline.C:
			return fmt.Errorf("sd-server not ready after %s", s.opts.ReadyTimeout)
		case <-tick.C:
			req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
			if err != nil {
				return err
			}
			resp, err := s.opts.HTTPClient.Do(req)
			if err != nil {
				continue
			}
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return nil
			}
		}
	}
}

func (s *SDServer) watchExit(name string, p proc.Process) {
	<-p.Done()
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.running != nil && s.running.proc == p {
		s.log.Warn("sd-server exited", "model", name, "err", p.Err())
		s.running = nil
	}
}

// LoadedImages implements ImageRunner.
func (s *SDServer) LoadedImages(context.Context) ([]LoadedImageModel, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.running == nil {
		return nil, nil
	}
	return []LoadedImageModel{s.running.lm}, nil
}

// UnloadImage implements ImageRunner.
func (s *SDServer) UnloadImage(_ context.Context, name string) error {
	s.mu.Lock()
	in := s.running
	if in == nil || in.lm.Name != name {
		s.mu.Unlock()
		return fmt.Errorf("%w: %s", ErrNotLoaded, name)
	}
	s.running = nil
	s.mu.Unlock()
	s.log.Info("stopping sd-server", "model", name, "pid", in.lm.PID)
	return in.proc.Stop(s.opts.StopTimeout)
}

// Close implements ImageRunner: stops a running server.
func (s *SDServer) Close(ctx context.Context) {
	s.mu.Lock()
	in := s.running
	s.mu.Unlock()
	if in != nil {
		if err := s.UnloadImage(ctx, in.lm.Name); err != nil && !errors.Is(err, ErrNotLoaded) {
			s.log.Warn("stop sd-server", "err", err)
		}
	}
}

var _ ImageRunner = (*SDServer)(nil)

// ensure the image model dir exists so a person can drop files in (best effort)
func (s *SDServer) EnsureDir() { _ = os.MkdirAll(s.modelDir(), 0o750) }
