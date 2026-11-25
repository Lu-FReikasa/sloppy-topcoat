// Package fake provides configurable fake providers for tests and `run --fake`.
//
// The fake GPU provider and fake model runner share one World so that loading
// a model raises the fake VRAM usage and loading beyond the total VRAM fails.
package fake

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"topcoat/node/internal/hf"
	"topcoat/node/internal/llamacpp"
	"topcoat/node/internal/providers/gpu"
	"topcoat/node/internal/providers/runner"
	"topcoat/node/internal/providers/sysinfo"
)

// Profile describes the fake machine. See fake-profile.example.json.
type Profile struct {
	CPU        ProfileCPU     `json:"cpu"`
	RAMTotalMB int64          `json:"ram_total_mb"`
	RAMUsedMB  int64          `json:"ram_used_mb"`
	GPUs       []ProfileGPU   `json:"gpus"`
	Models     []ProfileModel `json:"models"`
	// RunnerAvailable defaults to true.
	RunnerAvailable *bool `json:"runner_available,omitempty"`
	// Loaded lists models that are loaded at start.
	Loaded []string `json:"loaded,omitempty"`
}

// ProfileCPU is the fake CPU.
type ProfileCPU struct {
	Model string `json:"model"`
	Cores int    `json:"cores"`
	// Optional topology (defaults: threads = cores, all performance cores).
	Threads          int `json:"threads,omitempty"`
	PerformanceCores int `json:"performance_cores,omitempty"`
	EfficiencyCores  int `json:"efficiency_cores,omitempty"`
}

// ProfileGPU is one fake GPU.
type ProfileGPU struct {
	Name        string `json:"name"`
	Vendor      string `json:"vendor"`
	VRAMTotalMB int64  `json:"vram_total_mb"`
	// VRAMBaseUsedMB is used by "the desktop" even with no model loaded.
	VRAMBaseUsedMB int64 `json:"vram_base_used_mb"`
	// Integrated marks an iGPU; SharedTotalMB is its shared system memory.
	Integrated    bool  `json:"integrated,omitempty"`
	SharedTotalMB int64 `json:"shared_total_mb,omitempty"`
}

// ProfileModel is one fake model.
type ProfileModel struct {
	Name      string `json:"name"`
	SizeBytes int64  `json:"size_bytes"`
	Format    string `json:"format"`
	// VRAMMB is the VRAM the model occupies when loaded.
	VRAMMB int64 `json:"vram_mb"`
}

// DefaultProfile is a 16 GB AMD GPU, 32 GB RAM and three models of different sizes.
func DefaultProfile() Profile {
	return Profile{
		CPU:        ProfileCPU{Model: "Fake CPU 8-Core", Cores: 8},
		RAMTotalMB: 32768,
		RAMUsedMB:  9000,
		GPUs: []ProfileGPU{
			{Name: "Fake GPU 16G", Vendor: gpu.VendorAMD, VRAMTotalMB: 16384, VRAMBaseUsedMB: 800},
		},
		Models: []ProfileModel{
			{Name: "fake-llm-3b-q8", SizeBytes: 3_400_000_000, Format: "gguf", VRAMMB: 3600},
			{Name: "fake-llm-8b-q4", SizeBytes: 4_920_000_000, Format: "gguf", VRAMMB: 6100},
			{Name: "fake-llm-14b-q4", SizeBytes: 8_990_000_000, Format: "gguf", VRAMMB: 9800},
		},
	}
}

// LoadProfile reads a profile from a JSON file; unknown fields are rejected.
func LoadProfile(path string) (Profile, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return Profile{}, fmt.Errorf("read fake profile: %w", err)
	}
	return ParseProfile(b)
}

// ParseProfile parses and validates a profile.
func ParseProfile(b []byte) (Profile, error) {
	var p Profile
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&p); err != nil {
		return Profile{}, fmt.Errorf("parse fake profile: %w", err)
	}
	return p, p.Validate()
}

// Validate checks the profile for consistency.
func (p Profile) Validate() error {
	if p.CPU.Cores < 1 {
		return errors.New("fake profile: cpu.cores must be >= 1")
	}
	if p.RAMTotalMB < 0 || p.RAMUsedMB < 0 || p.RAMUsedMB > p.RAMTotalMB {
		return errors.New("fake profile: invalid ram values")
	}
	for i, g := range p.GPUs {
		switch g.Vendor {
		case gpu.VendorNVIDIA, gpu.VendorAMD, gpu.VendorIntel, gpu.VendorOther:
		default:
			return fmt.Errorf("fake profile: gpus[%d].vendor %q invalid", i, g.Vendor)
		}
		if g.VRAMTotalMB < 0 || g.VRAMBaseUsedMB < 0 || g.VRAMBaseUsedMB > g.VRAMTotalMB {
			return fmt.Errorf("fake profile: gpus[%d] invalid vram values", i)
		}
	}
	seen := map[string]bool{}
	for i, m := range p.Models {
		if m.Name == "" || seen[m.Name] {
			return fmt.Errorf("fake profile: models[%d] name empty or duplicate", i)
		}
		if m.VRAMMB < 0 || m.SizeBytes < 0 {
			return fmt.Errorf("fake profile: models[%d] negative size", i)
		}
		seen[m.Name] = true
	}
	for _, l := range p.Loaded {
		if !seen[l] {
			return fmt.Errorf("fake profile: loaded model %q not in models", l)
		}
	}
	return nil
}

type loadedEntry struct {
	gpu     int
	vram    int64
	ctxLen  int
	slots   int
	ordinal int
}

// Other simulates programs outside the node on one fake GPU (E1-06): VRAM
// they allocate, how much of our model memory the driver paged out to shared
// memory, their GPU utilization, and how much of a foreign program's memory the
// driver placed in shared memory because VRAM was full (unmet demand).
type Other struct {
	ForeignVRAMMB   int64   `json:"foreign_vram_mb"`
	DemotedMB       int64   `json:"demoted_mb"`
	ForeignUtil     float64 `json:"foreign_util"`
	ForeignSharedMB int64   `json:"foreign_shared_mb"`
}

// World is the shared fake machine state.
type World struct {
	mu        sync.Mutex
	profile   Profile
	loaded    map[string]loadedEntry
	available bool
	seq       int
	// others is per fake GPU; ramForeignMB is RAM used by other programs on
	// top of the profile's ram_used_mb.
	others       []Other
	ramForeignMB int64
	// cpuPercent is the CPU utilization the fake system reports (zero = cannot be measured).
	cpuPercent float64
	// DownloadStep / BenchDelay slow down simulated downloads and benchmark
	// runs so the GUI can show progress (tests set them to 0).
	DownloadStep time.Duration
	BenchDelay   time.Duration
	// BenchFailGPU makes every GPU benchmark run fail like Vulkan running out of device
	// memory on a model that does not fit; CPU runs still work (E2-05).
	BenchFailGPU bool
	// LoadDelay makes loads take time (concurrent loads of one model wait for
	// the first, like the llama-server runner); StreamDelay is the pause
	// between streamed chat tokens.
	LoadDelay   time.Duration
	StreamDelay time.Duration
	// APIKey, if set, is the key of the fake llama-server: requests without it get 401 (the
	// node must send it; the hub never sees it).
	APIKey string
	// ModelsDir, if set, is a real directory of <name>.gguf files: the fake runner lists them,
	// can send them to another node and receives files into it (E2-07). Set it before use.
	ModelsDir string
	// LoadFail, if set, is what every load ends with (tests of failed and late loads).
	LoadFail error
	// Loads counts loads that actually happened (tests check for duplicates).
	Loads   int
	loading map[string]chan struct{}
	// frozen is the "models are suspended" state (D24 step 2): streams stand still until thawed.
	frozen bool
	thawed chan struct{} // closed while not frozen
	// act is what the person at the fake machine does.
	act   fakeActivity
	slept int // times the fake machine went to sleep
}

// NewWorld creates a world from a profile (models in profile.Loaded are loaded).
func NewWorld(p Profile) (*World, error) {
	if err := p.Validate(); err != nil {
		return nil, err
	}
	w := &World{profile: p, loaded: map[string]loadedEntry{}, available: true,
		DownloadStep: 300 * time.Millisecond, BenchDelay: 700 * time.Millisecond,
		StreamDelay: 30 * time.Millisecond,
		others:      make([]Other, len(p.GPUs)), loading: map[string]chan struct{}{},
		thawed: make(chan struct{})}
	close(w.thawed)
	if p.RunnerAvailable != nil {
		w.available = *p.RunnerAvailable
	}
	for _, name := range p.Loaded {
		if _, err := w.load(name, runner.LoadOptions{}); err != nil {
			return nil, fmt.Errorf("fake profile: preload %s: %w", name, err)
		}
	}
	return w, nil
}

// isLoaded reports whether model is loaded.
func (w *World) isLoaded(model string) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	_, ok := w.loaded[model]
	return ok
}

// SetRunnerAvailable toggles fake runner health.
func (w *World) SetRunnerAvailable(ok bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.available = ok
}

// GPU returns the fake GPUProvider.
func (w *World) GPU() gpu.GPUProvider { return fakeGPU{w} }

// Runner returns the fake ModelRunner.
func (w *World) Runner() runner.ModelRunner { return fakeRunner{w} }

// SysInfo returns the fake sysinfo provider.
func (w *World) SysInfo() sysinfo.Provider { return fakeSys{w} }

// SetOther sets what other programs do on fake GPU idx.
func (w *World) SetOther(idx int, o Other) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if idx < 0 || idx >= len(w.others) {
		return fmt.Errorf("fake gpu %d does not exist", idx)
	}
	if o.ForeignVRAMMB < 0 || o.DemotedMB < 0 || o.ForeignSharedMB < 0 || o.ForeignUtil < 0 || o.ForeignUtil > 100 {
		return errors.New("fake: invalid other-program values")
	}
	w.others[idx] = o
	return nil
}

// SetCPU sets the CPU utilization (percent) the fake system reports; zero means the system cannot
// measure it (the default).
func (w *World) SetCPU(percent float64) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.cpuPercent = percent
}

// SetForeignRAM sets RAM used by other programs (on top of ram_used_mb).
func (w *World) SetForeignRAM(mb int64) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.ramForeignMB = max(mb, 0)
}

// ownOn is the VRAM our loaded models use on GPU idx.
func (w *World) ownOn(idx int) int64 {
	var own int64
	for _, l := range w.loaded {
		if l.gpu == idx {
			own += l.vram
		}
	}
	return own
}

// usedOn is everything allocated on GPU idx; paged-out (demoted) model memory
// no longer occupies dedicated VRAM.
func (w *World) usedOn(idx int) int64 {
	own := w.ownOn(idx)
	return w.profile.GPUs[idx].VRAMBaseUsedMB + w.others[idx].ForeignVRAMMB + own - min(w.others[idx].DemotedMB, own)
}

func (w *World) model(name string) (ProfileModel, bool) {
	for _, m := range w.profile.Models {
		if m.Name == name {
			return m, true
		}
	}
	return ProfileModel{}, false
}

func (w *World) load(name string, opts runner.LoadOptions) (runner.LoadedModel, error) {
	if w.LoadFail != nil {
		return runner.LoadedModel{}, w.LoadFail
	}
	m, ok := w.model(name)
	if !ok {
		return runner.LoadedModel{}, fmt.Errorf("%w: %s", runner.ErrUnknownModel, name)
	}
	if l, ok := w.loaded[name]; ok {
		return runner.LoadedModel{Name: name, VRAMUsedMB: l.vram, ContextLength: l.ctxLen, NParallel: l.slots}, nil
	}
	ctxLen := opts.ContextLength
	if ctxLen <= 0 && opts.MinContext > 0 {
		// "at least": the fake sizes like a roomy machine would, twice the floor up to 32768
		ctxLen = max(opts.MinContext, min(opts.MinContext*2, 32768))
	}
	for i, g := range w.profile.GPUs {
		capacity := g.VRAMTotalMB
		if g.Integrated {
			capacity += g.SharedTotalMB // an iGPU loads models into shared system memory
		}
		if capacity-w.usedOn(i) >= m.VRAMMB {
			w.seq++
			w.loaded[name] = loadedEntry{gpu: i, vram: m.VRAMMB, ctxLen: ctxLen, slots: max(opts.NParallel, 1), ordinal: w.seq}
			return runner.LoadedModel{Name: name, VRAMUsedMB: m.VRAMMB, ContextLength: ctxLen, NParallel: max(opts.NParallel, 1)}, nil
		}
	}
	return runner.LoadedModel{}, fmt.Errorf("not enough free VRAM to load %s (%d MB needed)", name, m.VRAMMB)
}

func (w *World) loadedList() []runner.LoadedModel {
	names := make([]string, 0, len(w.loaded))
	for n := range w.loaded {
		names = append(names, n)
	}
	sort.Slice(names, func(i, j int) bool { return w.loaded[names[i]].ordinal < w.loaded[names[j]].ordinal })
	out := make([]runner.LoadedModel, 0, len(names))
	for _, n := range names {
		l := w.loaded[n]
		out = append(out, runner.LoadedModel{Name: n, VRAMUsedMB: l.vram, ContextLength: l.ctxLen, NParallel: l.slots})
	}
	return out
}

var errRunnerDown = errors.New("fake runner is unavailable")

type fakeGPU struct{ w *World }

func (f fakeGPU) GPUs(context.Context) ([]gpu.GPU, error) {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	return f.w.gpus(), nil
}

func (w *World) gpus() []gpu.GPU {
	out := make([]gpu.GPU, 0, len(w.profile.GPUs))
	for i, g := range w.profile.GPUs {
		fg := gpu.GPU{Index: i, Name: g.Name, Vendor: g.Vendor, VRAMTotalMB: g.VRAMTotalMB, VRAMUsedMB: min(w.usedOn(i), g.VRAMTotalMB)}
		if g.Integrated {
			fg.Integrated, fg.SharedTotalMB = true, g.SharedTotalMB
			fg.VRAMUsedMB = g.VRAMBaseUsedMB
			fg.SharedUsedMB = w.usedOn(i) - g.VRAMBaseUsedMB
		}
		out = append(out, fg)
	}
	return out
}

// Sample implements gpu.Sampler: the fake world knows which memory is ours,
// so own usage and foreign utilization are always "measured".
func (f fakeGPU) Sample(_ context.Context, _ []int) ([]gpu.GPU, []gpu.ProcessUsage, error) {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	gpus := f.w.gpus()
	usage := make([]gpu.ProcessUsage, len(gpus))
	for i := range gpus {
		own, other := f.w.ownOn(i), f.w.others[i]
		demoted := min(other.DemotedMB, own)
		usage[i] = gpu.ProcessUsage{
			OwnDedicatedMB: own - demoted, OwnKnown: true,
			OwnSharedMB: demoted, SharedKnown: true,
			UtilPercent: other.ForeignUtil, UtilKnown: true,
			ForeignUtilPercent: other.ForeignUtil, ForeignUtilKnown: true,
			ForeignSharedMaxMB: other.ForeignSharedMB, ForeignSharedKnown: true,
		}
	}
	return gpus, usage, nil
}

type fakeSys struct{ w *World }

func (f fakeSys) Info(context.Context) (sysinfo.Info, error) {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	p := f.w.profile
	info := sysinfo.Info{CPUModel: p.CPU.Model, Cores: p.CPU.Cores, Threads: p.CPU.Threads,
		PerformanceCores: p.CPU.PerformanceCores, EfficiencyCores: p.CPU.EfficiencyCores,
		RAMTotalMB: p.RAMTotalMB, RAMUsedMB: min(p.RAMUsedMB+f.w.ramForeignMB, p.RAMTotalMB),
		CPUPercent: max(f.w.cpuPercent, 0), CPUKnown: f.w.cpuPercent > 0}
	if info.Threads == 0 {
		info.Threads = info.Cores
	}
	if info.PerformanceCores == 0 && info.EfficiencyCores == 0 {
		info.PerformanceCores = info.Cores
	}
	return info, nil
}

type fakeRunner struct{ w *World }

func (f fakeRunner) Type() string { return "fake" }

func (f fakeRunner) Health(context.Context) error {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	if !f.w.available {
		return errRunnerDown
	}
	return nil
}

func (f fakeRunner) ListModels(context.Context) ([]runner.Model, error) {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	if !f.w.available {
		return nil, errRunnerDown
	}
	out := make([]runner.Model, 0, len(f.w.profile.Models))
	for _, m := range f.w.profile.Models {
		out = append(out, runner.Model{Name: m.Name, SizeBytes: m.SizeBytes, Format: m.Format, Own: true})
	}
	if f.w.ModelsDir != "" {
		files, _ := filepath.Glob(filepath.Join(f.w.ModelsDir, "*.gguf"))
		for _, p := range files {
			name := strings.TrimSuffix(filepath.Base(p), ".gguf")
			if _, ok := f.w.model(name); ok {
				continue
			}
			if st, err := os.Stat(p); err == nil {
				out = append(out, runner.Model{Name: name, SizeBytes: st.Size(), Format: "gguf", Own: true})
			}
		}
	}
	return out, nil
}

// ModelFile implements runner.ModelFiler for the files in World.ModelsDir.
func (f fakeRunner) ModelFile(name string) (string, int64, bool, error) {
	f.w.mu.Lock()
	dir := f.w.ModelsDir
	f.w.mu.Unlock()
	if dir != "" && !strings.ContainsAny(name, `/\`) {
		p := filepath.Join(dir, name+".gguf")
		if st, err := os.Stat(p); err == nil && st.Mode().IsRegular() {
			return p, st.Size(), true, nil
		}
	}
	return "", 0, false, fmt.Errorf("%w: %s", runner.ErrUnknownModel, name)
}

// ReceiveDir implements runner.ModelReceiver; empty when World.ModelsDir is not set.
func (f fakeRunner) ReceiveDir() string {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	return f.w.ModelsDir
}

func (f fakeRunner) Loaded(context.Context) ([]runner.LoadedModel, error) {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	if !f.w.available {
		return nil, errRunnerDown
	}
	return f.w.loadedList(), nil
}

func (f fakeRunner) Load(ctx context.Context, name string, opts runner.LoadOptions) (runner.LoadedModel, error) {
	f.w.mu.Lock()
	if !f.w.available {
		f.w.mu.Unlock()
		return runner.LoadedModel{}, errRunnerDown
	}
	if _, ok := f.w.loaded[name]; ok || f.w.LoadDelay <= 0 {
		defer f.w.mu.Unlock()
		lm, err := f.w.load(name, opts)
		if err == nil && !ok {
			f.w.Loads++
		}
		return lm, err
	}
	if ch, busy := f.w.loading[name]; busy {
		// Another load of this model runs: wait for it instead of loading twice.
		f.w.mu.Unlock()
		select {
		case <-ch:
		case <-ctx.Done():
			return runner.LoadedModel{}, ctx.Err()
		}
		return f.Load(ctx, name, opts)
	}
	ch := make(chan struct{})
	f.w.loading[name] = ch
	delay := f.w.LoadDelay
	f.w.mu.Unlock()
	defer func() {
		f.w.mu.Lock()
		delete(f.w.loading, name)
		f.w.mu.Unlock()
		close(ch)
	}()
	select {
	case <-time.After(delay):
	case <-ctx.Done():
		return runner.LoadedModel{}, ctx.Err()
	}
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	lm, err := f.w.load(name, opts)
	if err == nil {
		f.w.Loads++
	}
	return lm, err
}

func (f fakeRunner) Unload(_ context.Context, name string) error {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	if !f.w.available {
		return errRunnerDown
	}
	if _, ok := f.w.loaded[name]; !ok {
		return fmt.Errorf("%w: %s", runner.ErrNotLoaded, name)
	}
	delete(f.w.loaded, name)
	return nil
}

func (f fakeRunner) UnloadAll(context.Context) ([]runner.LoadedModel, error) {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	if !f.w.available {
		return nil, errRunnerDown
	}
	out := f.w.loadedList()
	f.w.loaded = map[string]loadedEntry{}
	return out, nil
}

// Download simulates a Hugging Face download with progress; the model then
// appears with the file size as its VRAM need (+10 %).
func (f fakeRunner) Download(ctx context.Context, req runner.DownloadRequest) (runner.Model, error) {
	src := hf.Source{Repo: req.Repo, File: req.File, Revision: req.Revision}
	if err := src.Validate(); err != nil {
		return runner.Model{}, err
	}
	name := runner.DownloadName(req)
	f.w.mu.Lock()
	_, exists := f.w.model(name)
	step := f.w.DownloadStep
	f.w.mu.Unlock()
	if exists {
		return runner.Model{}, fmt.Errorf("%w: %s", runner.ErrModelExists, name)
	}
	const size = int64(1_100_000_000)
	for i := int64(1); i <= 5; i++ {
		if step > 0 {
			select {
			case <-ctx.Done():
				return runner.Model{}, ctx.Err()
			case <-time.After(step):
			}
		}
		if req.Progress != nil {
			req.Progress(size*i/5, size)
		}
	}
	m := ProfileModel{Name: name, SizeBytes: size, Format: "gguf", VRAMMB: size * 11 / 10 >> 20}
	f.w.mu.Lock()
	f.w.profile.Models = append(f.w.profile.Models, m)
	f.w.mu.Unlock()
	return runner.Model{Name: name, SizeBytes: size, Format: "gguf", Own: true}, nil
}

// Delete removes a model (unloading it first).
func (f fakeRunner) Delete(_ context.Context, name string) error {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	if _, ok := f.w.model(name); !ok {
		return fmt.Errorf("%w: %s", runner.ErrUnknownModel, name)
	}
	delete(f.w.loaded, name)
	models := f.w.profile.Models[:0:0]
	for _, m := range f.w.profile.Models {
		if m.Name != name {
			models = append(models, m)
		}
	}
	f.w.profile.Models = models
	return nil
}

// backend is what a real node would pick for the fake GPUs.
func (w *World) backend() string {
	vendors := make([]string, 0, len(w.profile.GPUs))
	for _, g := range w.profile.GPUs {
		vendors = append(vendors, g.Vendor)
	}
	return llamacpp.SelectBackend("auto", vendors)
}

// RuntimeInfo implements runner.Describer.
func (f fakeRunner) RuntimeInfo() runner.RuntimeInfo {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	return runner.RuntimeInfo{Backend: f.w.backend(), Release: "fake"}
}

// BenchTarget implements runner.Benchmarkable with a synthetic llama-bench:
// GPU runs are fast, CPU runs peak at the number of performance cores.
func (f fakeRunner) BenchTarget(context.Context) (runner.BenchTarget, error) {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	if !f.w.available {
		return runner.BenchTarget{}, errRunnerDown
	}
	pcores := f.w.profile.CPU.PerformanceCores
	if pcores == 0 {
		pcores = f.w.profile.CPU.Cores
	}
	w := f.w
	return runner.BenchTarget{
		Bench: "fake-llama-bench", Backend: w.backend(), Release: "fake",
		ModelPath: func(name string) (string, error) {
			w.mu.Lock()
			defer w.mu.Unlock()
			if _, ok := w.model(name); !ok {
				return "", fmt.Errorf("%w: %s", runner.ErrUnknownModel, name)
			}
			return "/fake/" + name + ".gguf", nil
		},
		Exec: func(ctx context.Context, _ string, args []string) ([]byte, error) {
			w.mu.Lock()
			delay := w.BenchDelay
			w.mu.Unlock()
			if delay > 0 {
				select {
				case <-ctx.Done():
					return nil, ctx.Err()
				case <-time.After(delay):
				}
			}
			if w.BenchFailGPU && !slices.Contains(args, "none") {
				return nil, fmt.Errorf("exit status 1: ggml_vulkan: Device memory allocation failed\nErrorOutOfDeviceMemory")
			}
			return fakeBench(args, pcores), nil
		},
	}, nil
}

func fakeBench(args []string, pcores int) []byte {
	threads, ngl := pcores, 0
	for i := 0; i+1 < len(args); i++ {
		switch args[i] {
		case "-t":
			threads, _ = strconv.Atoi(args[i+1])
		case "-ngl":
			ngl, _ = strconv.Atoi(args[i+1])
		case "-fitt": // a GPU run that lets llama.cpp fit the model to the device memory
			ngl = 99
		}
	}
	pp, tg := 40.0*float64(threads), 3.0*float64(min(threads, pcores))-0.5*float64(max(threads-pcores, 0))
	if ngl > 0 {
		pp, tg = 2500, 85
	}
	return []byte(fmt.Sprintf(`[{"n_prompt":512,"n_gen":0,"n_threads":%d,"avg_ts":%g},{"n_prompt":0,"n_gen":128,"n_threads":%d,"avg_ts":%g}]`,
		threads, pp, threads, max(tg, 1)))
}
