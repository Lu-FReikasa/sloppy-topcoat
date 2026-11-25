package bench

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/providers/runner"
)

// sample is shortened real `llama-bench -o json` output (see the llama.cpp README).
const sample = `ggml_vulkan: Found 1 Vulkan devices:
[
  {"build_commit": "8cf427ff", "backends": "Vulkan", "model_filename": "m.gguf", "n_threads": 8,
   "n_gpu_layers": 99, "n_prompt": 512, "n_gen": 0, "avg_ts": 7100.002165, "stddev_ts": 140.3},
  {"build_commit": "8cf427ff", "backends": "Vulkan", "model_filename": "m.gguf", "n_threads": 8,
   "n_gpu_layers": 99, "n_prompt": 0, "n_gen": 128, "avg_ts": 110.5, "stddev_ts": 1.2}
]`

func TestParse(t *testing.T) {
	r, err := Parse([]byte(sample))
	if err != nil {
		t.Fatal(err)
	}
	if r.PromptTPS != 7100.002165 || r.GenTPS != 110.5 || r.Threads != 8 {
		t.Fatalf("%+v", r)
	}
	if _, err := Parse([]byte("[]")); err == nil {
		t.Fatal("empty accepted")
	}
	if _, err := Parse([]byte("error: failed to load model")); err == nil {
		t.Fatal("garbage accepted")
	}
}

func TestThreadCandidates(t *testing.T) {
	cases := []struct {
		topo  Topology
		quick bool
		want  string
	}{
		{Topology{PerformanceCores: 2, EfficiencyCores: 8, Threads: 12}, false, "[2 6 10 12]"}, // i7-1255U
		{Topology{PerformanceCores: 2, EfficiencyCores: 8, Threads: 12}, true, "[2 12]"},
		{Topology{PerformanceCores: 8, Threads: 16}, false, "[8 16]"}, // Ryzen 7 3700X
		{Topology{PerformanceCores: 6, Threads: 6}, false, "[6]"},
		{Topology{}, false, "[0]"},
	}
	for _, c := range cases {
		if got := fmt.Sprint(ThreadCandidates(c.topo, c.quick)); got != c.want {
			t.Errorf("%+v quick=%v: %s want %s", c.topo, c.quick, got, c.want)
		}
	}
}

type call struct{ args []string }

func fakeTarget(backend string, calls *[]call, tgByThreads map[int]float64, fail error) runner.BenchTarget {
	return runner.BenchTarget{
		Bench: "/rt/llama-bench", Backend: backend, Threads: 0,
		ModelPath: func(n string) (string, error) {
			if n == "missing" {
				return "", runner.ErrUnknownModel
			}
			return "/models/" + n + ".gguf", nil
		},
		Exec: func(ctx context.Context, bin string, args []string) ([]byte, error) {
			*calls = append(*calls, call{args})
			if fail != nil {
				return nil, fail
			}
			threads, layers := 4, 0
			for i, a := range args {
				switch a {
				case "-t":
					threads, _ = strconv.Atoi(args[i+1])
				case "-ngl":
					layers, _ = strconv.Atoi(args[i+1])
				case "-fitt":
					layers = 35 // what llama.cpp's fit decided
				}
			}
			tg := tgByThreads[threads]
			if tg == 0 {
				tg = 50
			}
			return []byte(fmt.Sprintf(`[{"n_prompt":512,"n_gen":0,"avg_ts":300,"n_threads":%d,"n_gpu_layers":%d},{"n_prompt":0,"n_gen":128,"avg_ts":%g,"n_threads":%d,"n_gpu_layers":%d}]`, threads, layers, tg, threads, layers)), nil
		},
	}
}

func TestIGPUComparesVulkanAndCPUSweep(t *testing.T) {
	var calls []call
	target := fakeTarget("vulkan", &calls, map[int]float64{2: 9, 6: 14, 10: 13, 12: 11}, nil)
	var emitted []Result
	sum, err := Run(context.Background(), target, Spec{
		Models: []string{"qwen"}, IntegratedGPU: true,
		Topology: Topology{PerformanceCores: 2, EfficiencyCores: 8, Threads: 12},
	}, func(r Result) { emitted = append(emitted, r) })
	if err != nil {
		t.Fatal(err)
	}
	if len(emitted) != 5 || emitted[0].Backend != "vulkan" || emitted[0].GPULayers != 35 {
		t.Fatalf("%+v", emitted)
	}
	for _, r := range emitted[1:] {
		if r.Backend != "cpu" || r.GPULayers != 0 {
			t.Fatalf("%+v", r)
		}
	}
	if sum.RecommendedThreads != 6 {
		t.Fatalf("recommended %d", sum.RecommendedThreads)
	}
	gpuArgs := strings.Join(calls[0].args, " ")
	cpuArgs := strings.Join(calls[1].args, " ")
	// a GPU run lets llama.cpp fit the model to the device memory: no -ngl, a fit target
	if !strings.Contains(gpuArgs, "-m /models/qwen.gguf -p 512 -n 128 -r 3 -o json -fitt 1024") ||
		strings.Contains(gpuArgs, "-dev none") || strings.Contains(gpuArgs, "-ngl") {
		t.Fatalf("gpu args %s", gpuArgs)
	}
	if !strings.Contains(cpuArgs, "-ngl 0 -t 2 -dev none") {
		t.Fatalf("cpu args %s", cpuArgs)
	}
	d := emitted[1].Data("id-1")
	if d["benchmark_id"] != "id-1" || d["tg_tps"] != 9.0 || d["threads"] != 2 {
		t.Fatalf("%v", d)
	}
}

func TestDedicatedGPUOnlyGPURunAndQuick(t *testing.T) {
	var calls []call
	sum, err := Run(context.Background(), fakeTarget("cuda", &calls, nil, nil), Spec{
		Models: []string{"a", "b"}, Quick: true, Topology: Topology{PerformanceCores: 6, Threads: 12},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(sum.Results) != 2 || sum.RecommendedThreads != 0 || !strings.Contains(strings.Join(calls[0].args, " "), "-r 1") {
		t.Fatalf("%+v %v", sum, calls)
	}
}

func TestCPUBackendSweepsAcrossModels(t *testing.T) {
	var calls []call
	sum, err := Run(context.Background(), fakeTarget("cpu", &calls, map[int]float64{8: 20, 16: 18}, nil), Spec{
		Models: []string{"a", "b"}, Topology: Topology{PerformanceCores: 8, Threads: 16},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(sum.Results) != 4 || sum.RecommendedThreads != 8 {
		t.Fatalf("%+v", sum)
	}
}

func TestErrorsAndAbort(t *testing.T) {
	var calls []call
	if _, err := Run(context.Background(), fakeTarget("cpu", &calls, nil, nil), Spec{}, nil); err == nil {
		t.Fatal("no models accepted")
	}
	if _, err := Run(context.Background(), fakeTarget("cpu", &calls, nil, nil), Spec{Models: []string{"missing"}}, nil); !errors.Is(err, runner.ErrUnknownModel) {
		t.Fatalf("got %v", err)
	}
	if _, err := Run(context.Background(), fakeTarget("cuda", &calls, nil, errors.New("exit status 1: out of memory")), Spec{Models: []string{"a"}}, nil); err == nil || !strings.Contains(err.Error(), "out of memory") {
		t.Fatalf("every run failed must be an error, got %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	target := fakeTarget("cpu", &calls, nil, nil)
	inner := target.Exec
	n := 0
	target.Exec = func(c context.Context, b string, a []string) ([]byte, error) {
		n++
		if n == 2 {
			cancel() // a game started during the second run
		}
		return inner(c, b, a)
	}
	sum, err := Run(ctx, target, Spec{Models: []string{"a"}, Topology: Topology{PerformanceCores: 8, Threads: 16}}, nil)
	if !errors.Is(err, context.Canceled) || len(sum.Results) != 1 {
		t.Fatalf("%+v %v", sum, err)
	}
}

func TestAFailingBackendDoesNotAbortTheRest(t *testing.T) {
	// Vulkan runs out of device memory on the big model; the CPU sweep still runs (E2-05 A1).
	var calls []call
	target := fakeTarget("vulkan", &calls, map[int]float64{2: 9, 6: 14}, nil)
	inner := target.Exec
	target.Exec = func(ctx context.Context, bin string, args []string) ([]byte, error) {
		if !slices.Contains(args, "-dev") { // the GPU run
			calls = append(calls, call{args})
			return nil, errors.New("exit status 1: ggml_vulkan: Device memory allocation failed\nErrorOutOfDeviceMemory\nlots of log")
		}
		return inner(ctx, bin, args)
	}
	var failed []RunError
	var got []Result
	sum, err := Run(context.Background(), target, Spec{
		Models: []string{"big"}, IntegratedGPU: true, Quick: true,
		Topology:  Topology{PerformanceCores: 2, EfficiencyCores: 8, Threads: 12},
		OnFailure: func(e RunError) { failed = append(failed, e) },
	}, func(r Result) { got = append(got, r) })
	if err != nil {
		t.Fatalf("a failed backend with other measurements is not an error: %v", err)
	}
	if len(failed) != 1 || failed[0].Backend != "vulkan" || failed[0].GPULayers != -1 ||
		failed[0].Err != "exit status 1: ggml_vulkan: Device memory allocation failed" {
		t.Fatalf("%+v", failed)
	}
	if len(got) != 2 || got[0].Backend != "cpu" || len(sum.Failures) != 1 || sum.RecommendedThreads == 0 {
		t.Fatalf("%+v %+v", got, sum)
	}
	d := failed[0].Data("id")
	if _, has := d["n_gpu_layers"]; has || d["error"] == "" || d["backend"] != "vulkan" {
		t.Fatalf("%v", d)
	}
}

func TestModelsCanUseDifferentBackendsAndFixedLayers(t *testing.T) {
	var calls []call
	target := fakeTarget("vulkan", &calls, nil, nil)
	zero, ten := 0, 10
	bins := map[string]string{}
	target.For = func(model string) (runner.ModelBench, error) {
		switch model {
		case "small":
			return runner.ModelBench{Bench: "/vk/llama-bench", Backend: "vulkan"}, nil
		case "big":
			return runner.ModelBench{Bench: "/cpu/llama-bench", Backend: "cpu"}, nil
		case "half":
			return runner.ModelBench{Bench: "/vk/llama-bench", Backend: "vulkan", GPULayers: &ten}, nil
		case "none":
			return runner.ModelBench{Bench: "/vk/llama-bench", Backend: "vulkan", GPULayers: &zero}, nil
		}
		return runner.ModelBench{}, runner.ErrUnknownModel
	}
	inner := target.Exec
	target.Exec = func(ctx context.Context, bin string, args []string) ([]byte, error) {
		bins[strings.Join(args[1:2], "")] = bin
		return inner(ctx, bin, args)
	}
	sum, err := Run(context.Background(), target, Spec{
		Models: []string{"small", "big", "half", "none"}, Quick: true, Topology: Topology{PerformanceCores: 4, Threads: 8},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	by := map[string][]Result{}
	for _, r := range sum.Results {
		by[r.Model] = append(by[r.Model], r)
	}
	if r := by["small"]; len(r) != 1 || r[0].Backend != "vulkan" || r[0].GPULayers != 35 {
		t.Fatalf("small: %+v", r)
	}
	if r := by["big"]; len(r) != 2 || r[0].Backend != "cpu" || r[0].GPULayers != 0 {
		t.Fatalf("big must be a CPU sweep: %+v", r)
	}
	if r := by["half"]; len(r) != 1 || r[0].GPULayers != 10 {
		t.Fatalf("half: %+v", r)
	}
	if r := by["none"]; len(r) != 1 || r[0].GPULayers != 0 {
		t.Fatalf("none: %+v", r)
	}
	var sawCPUBin, sawFixed bool
	for _, c := range calls {
		joined := strings.Join(c.args, " ")
		if strings.Contains(joined, "/models/half.gguf") {
			sawFixed = strings.Contains(joined, "-ngl 10") && !strings.Contains(joined, "-fitt")
		}
		if strings.Contains(joined, "/models/big.gguf") && !strings.Contains(joined, "-dev none") {
			t.Fatalf("big must run on the CPU only: %s", joined)
		}
	}
	for _, b := range bins {
		sawCPUBin = sawCPUBin || b == "/cpu/llama-bench"
	}
	if !sawFixed || !sawCPUBin {
		t.Fatalf("fixed=%v cpubin=%v", sawFixed, sawCPUBin)
	}
	if _, err := Run(context.Background(), target, Spec{Models: []string{"ghost"}}, nil); err == nil {
		t.Fatal("unknown model accepted")
	}
}

func TestParseReadsTheLayersLlamaBenchReports(t *testing.T) {
	r, err := Parse([]byte(sample))
	if err != nil || r.GPULayers != 99 {
		t.Fatalf("%+v %v", r, err)
	}
	r, err = Parse([]byte(`[{"n_prompt":512,"n_gen":0,"avg_ts":10,"n_threads":4}]`))
	if err != nil || r.GPULayers != -1 {
		t.Fatalf("unknown layers must be -1: %+v %v", r, err)
	}
	if _, has := r.Data("x")["n_gpu_layers"]; has {
		t.Fatal("unknown layers must not be reported")
	}
}

func TestFitRunUsesTheRunnersMargin(t *testing.T) {
	// the benchmark must leave as much memory free as a load does: an iGPU gets a large margin
	var calls []call
	target := fakeTarget("vulkan", &calls, nil, nil)
	target.For = func(string) (runner.ModelBench, error) {
		return runner.ModelBench{Bench: "/rt/llama-bench", Backend: "vulkan", FitMarginMB: 4096}, nil
	}
	if _, err := Run(context.Background(), target, Spec{Models: []string{"qwen"}, Quick: true}, nil); err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(calls[0].args, " "); !strings.Contains(got, "-fitt 4096") {
		t.Fatalf("args %s", got)
	}
}

func TestExpertOffloadIsMeasuredWithTheConfiguredValue(t *testing.T) {
	var calls []call
	target := fakeTarget("vulkan", &calls, nil, nil)
	twelve := 12
	target.For = func(string) (runner.ModelBench, error) {
		return runner.ModelBench{Bench: "/vk/llama-bench", Backend: "vulkan", NCPUMoe: &twelve}, nil
	}
	if _, err := Run(context.Background(), target, Spec{Models: []string{"moe"}, Quick: true}, nil); err != nil {
		t.Fatal(err)
	}
	if len(calls) != 1 {
		t.Fatalf("%d runs", len(calls))
	}
	a := strings.Join(calls[0].args, " ")
	if !strings.Contains(a, "-ncmoe 12") || !strings.Contains(a, "-ngl 999") || strings.Contains(a, "-fitt") {
		t.Errorf("%s", a)
	}
}

func TestAHungRunTimesOutAndTheRestGoesOn(t *testing.T) {
	var calls []call
	target := fakeTarget("vulkan", &calls, map[int]float64{2: 9, 6: 14}, nil)
	inner := target.Exec
	target.Exec = func(ctx context.Context, bin string, args []string) ([]byte, error) {
		if !slices.Contains(args, "-dev") { // the GPU run hangs until it is told to stop
			<-ctx.Done()
			return nil, ctx.Err()
		}
		return inner(ctx, bin, args)
	}
	var failed []RunError
	sum, err := Run(context.Background(), target, Spec{
		Models: []string{"big"}, IntegratedGPU: true, Quick: true,
		Topology:   Topology{PerformanceCores: 2, EfficiencyCores: 8, Threads: 12},
		RunTimeout: 50 * time.Millisecond,
		OnFailure:  func(e RunError) { failed = append(failed, e) },
	}, nil)
	if err != nil {
		t.Fatalf("a timed out run with other measurements is not an error: %v", err)
	}
	if len(failed) != 1 || !strings.Contains(failed[0].Err, "timed out after 50ms") || len(sum.Results) != 2 {
		t.Fatalf("%+v %+v", failed, sum)
	}
}

func TestACanceledBenchmarkIsNotReportedAsATimeout(t *testing.T) {
	var calls []call
	target := fakeTarget("cpu", &calls, nil, nil)
	ctx, cancel := context.WithCancel(context.Background())
	target.Exec = func(c context.Context, bin string, args []string) ([]byte, error) {
		cancel()
		<-c.Done()
		return nil, c.Err()
	}
	_, err := Run(ctx, target, Spec{Models: []string{"a"}, RunTimeout: time.Minute}, nil)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("got %v", err)
	}
}
