// Package bench runs llama-bench from the node's pinned llama.cpp build:
// prompt processing (pp512) and generation (tg128) per model and backend, a
// thread sweep for CPU work and, on iGPU machines, GPU vs. CPU.
package bench

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"topcoat/node/internal/providers/runner"
)

// Topology is the CPU core layout used for the thread sweep.
type Topology struct {
	PerformanceCores int // physical P-cores (all cores on non-hybrid CPUs)
	EfficiencyCores  int // physical E-cores
	Threads          int // logical processors
}

// Spec is one benchmark request.
type Spec struct {
	Models []string
	Quick  bool
	// IntegratedGPU: also measure the CPU next to the GPU backend.
	IntegratedGPU bool
	Topology      Topology
	// OnFailure is called for each failed run, as soon as it failed (may be nil).
	OnFailure func(RunError)
	// RunTimeout ends one llama-bench run that hangs (a model that thrashes memory, a driver that
	// stalls); the run counts as failed and the benchmark goes on. 0 = DefaultRunTimeout.
	RunTimeout time.Duration
}

// DefaultRunTimeout is the longest one llama-bench run may take (three repetitions of pp512 and
// tg128 take seconds to a few minutes, also for a large model that is partly in RAM).
const DefaultRunTimeout = 10 * time.Minute

// RunError is one llama-bench run that failed (e.g. Vulkan: out of device memory). The
// benchmark goes on with the remaining runs.
type RunError struct {
	Model     string
	Backend   string
	Threads   int
	GPULayers int
	Err       string
}

// Data returns the benchmark_error event data.
func (e RunError) Data(id string) map[string]any {
	d := map[string]any{
		"benchmark_id": id, "model": e.Model, "backend": e.Backend, "threads": e.Threads, "error": e.Err,
	}
	if e.GPULayers >= 0 {
		d["n_gpu_layers"] = e.GPULayers
	}
	return d
}

// Result is one measurement.
type Result struct {
	Model     string
	Backend   string
	Threads   int
	GPULayers int     // layers on the GPU as llama-bench reports them; -1 = not known (fit)
	PromptTPS float64 // pp512
	GenTPS    float64 // tg128
}

// Data returns the benchmark_result event data.
func (r Result) Data(id string) map[string]any {
	d := map[string]any{
		"benchmark_id": id, "model": r.Model, "backend": r.Backend, "threads": r.Threads,
		"pp_tps": round2(r.PromptTPS), "tg_tps": round2(r.GenTPS),
	}
	if r.GPULayers >= 0 {
		d["n_gpu_layers"] = r.GPULayers
	}
	return d
}

func round2(v float64) float64 { return float64(int64(v*100+0.5)) / 100 }

// Summary is the outcome of a run.
type Summary struct {
	Results []Result
	// Failures are the runs that failed; the others were measured anyway.
	Failures []RunError
	// RecommendedThreads is the fastest CPU thread count (0 = no sweep ran).
	RecommendedThreads int
}

// ThreadCandidates returns the thread counts to try: P-cores only, P-cores +
// half the E-cores, all physical cores, all logical threads (deduplicated,
// ascending). Quick keeps the first and the last.
func ThreadCandidates(t Topology, quick bool) []int {
	phys := t.PerformanceCores + t.EfficiencyCores
	set := map[int]bool{}
	add := func(n int) {
		if n > 0 {
			set[n] = true
		}
	}
	add(t.PerformanceCores)
	if t.EfficiencyCores > 0 {
		add(t.PerformanceCores + t.EfficiencyCores/2)
	}
	add(phys)
	add(t.Threads)
	out := make([]int, 0, len(set))
	for n := range set {
		out = append(out, n)
	}
	sort.Ints(out)
	if len(out) == 0 {
		return []int{0} // llama.cpp default
	}
	if quick && len(out) > 2 {
		out = []int{out[0], out[len(out)-1]}
	}
	return out
}

type run struct {
	bench   string
	backend string
	threads int
	// gpuLayers is the fixed number of layers (-ngl); fit means "as many as the device
	// memory holds" (llama-bench -fitt), which also measures a partial offload.
	gpuLayers int
	fit       bool
	cpuOnly   bool
	nCPUMoe   *int // -ncmoe: MoE experts of the first N layers stay in RAM
}

// fitMarginMB is the memory left free on the device by a fit run unless the runner names another
// (llama.cpp's default; an iGPU gets more, so the benchmark measures what the server will do).
// UNVERIFIED on real hardware: `llama-bench -fitt` without -ngl (its --help lists both; whether
// a run reports the layers the fit chose in n_gpu_layers is what the hardware test checks).
const fitMarginMB = 1024

// modelBench resolves how one model is benchmarked.
func modelBench(target runner.BenchTarget, model string) (runner.ModelBench, error) {
	if target.For != nil {
		return target.For(model)
	}
	return runner.ModelBench{Bench: target.Bench, Backend: target.Backend}, nil
}

// plan lists the llama-bench runs of one model: a CPU model gets the thread sweep; a GPU
// model one run with llama.cpp's fit (or the configured number of layers) and, on an iGPU
// machine, the CPU sweep next to it.
func plan(target runner.BenchTarget, mb runner.ModelBench, spec Spec) []run {
	cpuSweep := func() []run {
		var rs []run
		for _, n := range ThreadCandidates(spec.Topology, spec.Quick) {
			rs = append(rs, run{bench: mb.Bench, backend: "cpu", threads: n, gpuLayers: 0, cpuOnly: true})
		}
		return rs
	}
	if mb.Backend == "cpu" {
		return cpuSweep()
	}
	gpu := run{bench: mb.Bench, backend: mb.Backend, threads: target.Threads}
	gpu.nCPUMoe = mb.NCPUMoe
	switch {
	case mb.GPULayers != nil:
		gpu.gpuLayers = *mb.GPULayers
	case mb.NCPUMoe != nil:
		gpu.gpuLayers = 999 // as a load does: all layers on the GPU, the experts in RAM (-ngl all)
	default:
		gpu.fit = true
	}
	if spec.IntegratedGPU {
		return append([]run{gpu}, cpuSweep()...)
	}
	return []run{gpu}
}

// Emit receives each result as soon as it is measured.
type Emit func(Result)

// Run measures every model. A run that fails (a backend that cannot load the model, out of
// device memory …) is recorded in Summary.Failures and reported through Spec.OnFailure; the
// other runs still happen. Run returns an error only when ctx is canceled (e.g. a game
// started), a model is unknown, or every run failed; it always returns what it measured.
func Run(ctx context.Context, target runner.BenchTarget, spec Spec, emit Emit) (Summary, error) {
	if len(spec.Models) == 0 {
		return Summary{}, errors.New("no models given")
	}
	reps := "3"
	if spec.Quick {
		reps = "1"
	}
	var sum Summary
	cpuTG := map[int]float64{}
	for _, model := range spec.Models {
		path, err := target.ModelPath(model)
		if err != nil {
			return sum, err
		}
		mb, err := modelBench(target, model)
		if err != nil {
			return sum, fmt.Errorf("benchmark %s: %w", model, err)
		}
		for _, r := range plan(target, mb, spec) {
			if err := ctx.Err(); err != nil {
				return sum, err
			}
			args := []string{"-m", path, "-p", "512", "-n", "128", "-r", reps, "-o", "json"}
			if r.fit {
				margin := int64(fitMarginMB)
				if mb.FitMarginMB > 0 {
					margin = mb.FitMarginMB
				}
				args = append(args, "-fitt", strconv.FormatInt(margin, 10))
			} else {
				args = append(args, "-ngl", strconv.Itoa(r.gpuLayers))
			}
			if r.nCPUMoe != nil {
				args = append(args, "-ncmoe", strconv.Itoa(*r.nCPUMoe))
			}
			if r.threads > 0 {
				args = append(args, "-t", strconv.Itoa(r.threads))
			}
			if r.cpuOnly {
				args = append(args, "-dev", "none")
			}
			limit := spec.RunTimeout
			if limit <= 0 {
				limit = DefaultRunTimeout
			}
			rctx, stop := context.WithTimeout(ctx, limit)
			out, err := target.Exec(rctx, r.bench, args)
			timedOut := rctx.Err() != nil && ctx.Err() == nil
			stop()
			if ctx.Err() != nil {
				return sum, ctx.Err()
			}
			if timedOut {
				err = fmt.Errorf("timed out after %s", limit)
			}
			var res Result
			if err == nil {
				res, err = Parse(out)
			}
			if err != nil {
				layers := r.gpuLayers
				if r.fit {
					layers = -1
				}
				f := RunError{Model: model, Backend: r.backend, Threads: r.threads, GPULayers: layers, Err: firstLine(err.Error())}
				sum.Failures = append(sum.Failures, f)
				if spec.OnFailure != nil {
					spec.OnFailure(f)
				}
				continue
			}
			res.Model, res.Backend = model, r.backend
			if !r.fit {
				res.GPULayers = r.gpuLayers
			}
			if res.Threads == 0 {
				res.Threads = r.threads
			}
			sum.Results = append(sum.Results, res)
			if r.cpuOnly {
				cpuTG[res.Threads] += res.GenTPS
			}
			if emit != nil {
				emit(res)
			}
		}
	}
	if len(sum.Results) == 0 && len(sum.Failures) > 0 {
		f := sum.Failures[0]
		return sum, fmt.Errorf("every benchmark run failed; first: %s (%s): %s", f.Model, f.Backend, f.Err)
	}
	if len(cpuTG) > 1 {
		best, bestTG := 0, -1.0
		for n, tg := range cpuTG {
			if tg > bestTG || (tg == bestTG && n < best) {
				best, bestTG = n, tg
			}
		}
		sum.RecommendedThreads = best
	}
	return sum, nil
}

// firstLine keeps the first non-empty line of an error (llama-bench appends its log).
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

// Parse reads `llama-bench -o json` output: one entry with n_prompt>0 (pp)
// and one with n_gen>0 (tg).
func Parse(out []byte) (Result, error) {
	// llama-bench may print log lines before the JSON array.
	s := string(out)
	if i := strings.Index(s, "["); i > 0 {
		s = s[i:]
	}
	var entries []struct {
		NPrompt   int     `json:"n_prompt"`
		NGen      int     `json:"n_gen"`
		NThreads  int     `json:"n_threads"`
		NGPULayer *int    `json:"n_gpu_layers"`
		AvgTS     float64 `json:"avg_ts"`
	}
	if err := json.Unmarshal([]byte(s), &entries); err != nil {
		return Result{}, fmt.Errorf("parse llama-bench json: %w", err)
	}
	r := Result{GPULayers: -1}
	for _, e := range entries {
		if e.NGPULayer != nil && *e.NGPULayer >= 0 {
			r.GPULayers = *e.NGPULayer
		}
		switch {
		case e.NPrompt > 0 && e.NGen == 0:
			r.PromptTPS = e.AvgTS
		case e.NGen > 0 && e.NPrompt == 0:
			r.GenTPS = e.AvgTS
		}
		if e.NThreads > 0 {
			r.Threads = e.NThreads
		}
	}
	if r.PromptTPS == 0 && r.GenTPS == 0 {
		return r, errors.New("llama-bench returned no pp/tg results")
	}
	return r, nil
}
