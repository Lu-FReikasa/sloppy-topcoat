package runner

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"topcoat/node/internal/diskfree"
	"topcoat/node/internal/hf"
	"topcoat/node/internal/llamacpp"
	"topcoat/node/internal/proc"
	"topcoat/node/internal/providers/gpu"
	"topcoat/node/internal/providers/sysinfo"
)

// fakeServer imitates a llama-server process: it listens on the --port from
// its arguments, answers /health with 503 for a few polls, then 200.
type fakeServer struct {
	args    []string
	key     string // content of the --api-key-file at start
	srv     *http.Server
	done    chan struct{}
	err     error
	stopped atomic.Bool
	once    sync.Once
	// suspended / suspends: the process is frozen now / was frozen this many times (D24)
	suspended atomic.Bool
	killed    atomic.Bool
	// slowDeath: Kill only ends the process when the channel is closed (the OS still tears it down)
	slowDeath chan struct{}
	// stopErr: Stop cannot end the process
	stopErr  error
	suspends atomic.Int32
}

func (f *fakeServer) Suspend() error { f.suspended.Store(true); f.suspends.Add(1); return nil }
func (f *fakeServer) Resume() error  { f.suspended.Store(false); return nil }
func (f *fakeServer) Kill() error {
	f.killed.Store(true)
	if f.slowDeath != nil {
		go func() { <-f.slowDeath; f.exit(errors.New("signal: killed")) }()
		return nil
	}
	f.exit(errors.New("signal: killed"))
	return nil
}
func (f *fakeServer) PID() int              { return 4242 }
func (f *fakeServer) Done() <-chan struct{} { return f.done }
func (f *fakeServer) Err() error            { return f.err }
func (f *fakeServer) RSSMB() int64          { return 321 }
func (f *fakeServer) Stop(time.Duration) error {
	f.stopped.Store(true)
	if f.stopErr != nil {
		return f.stopErr
	}
	f.exit(errors.New("signal: terminated"))
	return nil
}

func (f *fakeServer) exit(err error) {
	f.once.Do(func() {
		f.err = err
		_ = f.srv.Close()
		close(f.done)
	})
}

type fakeStarter struct {
	mu        sync.Mutex
	started   []*fakeServer
	failStart bool
	neverUp   bool
	log       string
	onStart   func()
	// failWhen returns log text for a start that dies of it at once (an allocation failure).
	failWhen func(args []string) string
	// configure adjusts each fake server right after it was created (slow death, stop errors)
	configure func(f *fakeServer)
}

func (fs *fakeStarter) start(_ context.Context, bin string, args []string, _ string, out io.Writer) (proc.Process, error) {
	if fs.failStart {
		return nil, errors.New("exec format error")
	}
	port := ""
	for i, a := range args {
		if a == "--port" {
			port = args[i+1]
		}
	}
	ln, err := net.Listen("tcp", "127.0.0.1:"+port)
	if err != nil {
		return nil, err
	}
	var polls atomic.Int32
	f := &fakeServer{args: append([]string{bin}, args...), done: make(chan struct{})}
	if fs.configure != nil {
		fs.configure(f)
	}
	if kf, ok := argOf(args, "--api-key-file"); ok {
		b, _ := os.ReadFile(kf)
		f.key = strings.TrimSpace(string(b))
	}
	f.srv = &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/health" || fs.neverUp || polls.Add(1) < 3 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	}), ReadHeaderTimeout: time.Second}
	go func() { _ = f.srv.Serve(ln) }()
	_, _ = io.WriteString(out, fs.log)
	if fs.failWhen != nil {
		if text := fs.failWhen(args); text != "" {
			_, _ = io.WriteString(out, text)
			f.exit(errors.New("exit status 1"))
		}
	}
	if fs.onStart != nil {
		fs.onStart()
	}
	fs.mu.Lock()
	fs.started = append(fs.started, f)
	fs.mu.Unlock()
	return f, nil
}

const cudaLog = `llama_context: n_ctx         = 4096
llama_context: n_ctx_seq     = 4096
load_tensors:        CUDA0 model buffer size =  1000.40 MiB
load_tensors:   CPU_Mapped model buffer size =   200.00 MiB
llama_kv_cache:      CUDA0 KV buffer size =   224.00 MiB
sched_reserve:      CUDA0 compute buffer size =   300.00 MiB
sched_reserve:  CUDA_Host compute buffer size =    12.00 MiB
`

type fixedGPU struct{ used atomic.Int64 }

func (g *fixedGPU) GPUs(context.Context) ([]gpu.GPU, error) {
	return []gpu.GPU{{Index: 0, Name: "g", Vendor: "nvidia", VRAMTotalMB: 8192, VRAMUsedMB: g.used.Load()}}, nil
}

// installed prepares a fake installed runtime.
func installed(t *testing.T, data, backend string) *llamacpp.Installer {
	t.Helper()
	in := &llamacpp.Installer{Root: filepath.Join(data, "runtime"), Manifest: llamacpp.Pinned()}
	dir := filepath.Join(in.Root, "b1-"+backend)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	server := filepath.Join(dir, "llama-server")
	_ = os.WriteFile(server, []byte("#!"), 0o755)
	marker := fmt.Sprintf(`{"release":"b1","backend":%q,"dir":%q,"server":%q,"bench":%q}`, backend, dir, server, filepath.Join(dir, "llama-bench"))
	if err := os.WriteFile(filepath.Join(dir, "topcoat-install.json"), []byte(marker), 0o644); err != nil {
		t.Fatal(err)
	}
	return in
}

func writeModel(t *testing.T, dir, name string, size int) {
	t.Helper()
	_ = os.MkdirAll(dir, 0o755)
	if err := os.WriteFile(filepath.Join(dir, name), make([]byte, size), 0o644); err != nil {
		t.Fatal(err)
	}
}

func newTestServer(t *testing.T, backend string, fs *fakeStarter, mutate ...func(*LlamaServerOptions)) (*LlamaServer, string) {
	t.Helper()
	data := t.TempDir()
	opts := LlamaServerOptions{
		DataDir: data, Backend: backend, Release: "b1", Threads: 6,
		Installer: installed(t, data, backend), Start: fs.start,
		PollInterval: 5 * time.Millisecond, ReadyTimeout: 5 * time.Second,
	}
	for _, m := range mutate {
		m(&opts)
	}
	l := NewLlamaServer(opts)
	t.Cleanup(func() { l.Close(context.Background()) })
	waitHealthy(t, l)
	return l, data
}

func waitHealthy(t *testing.T, l *LlamaServer) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for l.Health(context.Background()) != nil {
		if time.Now().After(deadline) {
			t.Fatalf("runner not healthy: %v", l.Health(context.Background()))
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestModelScanning(t *testing.T) {
	extra := t.TempDir()
	l, data := newTestServer(t, "cpu", &fakeStarter{}, func(o *LlamaServerOptions) { o.ModelDirs = []string{extra, "", "/does/not/exist"} })
	writeModel(t, filepath.Join(data, "models"), "a-q4.gguf", 10)
	writeModel(t, filepath.Join(extra, "pub", "repo"), "b-q8.GGUF", 20)
	writeModel(t, filepath.Join(extra, "pub", "repo"), "mmproj-b.gguf", 5)
	writeModel(t, extra, "big-00001-of-00002.gguf", 7)
	writeModel(t, extra, "big-00002-of-00002.gguf", 7)
	writeModel(t, extra, "a-q4.gguf", 99) // shadowed by the node's own copy
	writeModel(t, extra, "notes.txt", 1)
	writeModel(t, filepath.Join(extra, "1", "2", "3", "4", "5"), "deep.gguf", 1)
	models, err := l.ListModels(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, m := range models {
		got = append(got, fmt.Sprintf("%s:%d", m.Name, m.SizeBytes))
	}
	if strings.Join(got, ",") != "a-q4:10,b-q8:20,big:7" {
		t.Fatalf("%v", got)
	}
}

func TestLoadUnloadWithMeasuredMemory(t *testing.T) {
	fs := &fakeStarter{log: cudaLog}
	g := &fixedGPU{}
	l, data := newTestServer(t, "cuda", fs, func(o *LlamaServerOptions) { o.GPU = g })
	writeModel(t, filepath.Join(data, "models"), "m.gguf", 10)
	ctx := context.Background()

	lm, err := l.Load(ctx, "m", LoadOptions{ContextLength: 4096})
	if err != nil {
		t.Fatal(err)
	}
	if lm.PID != 4242 || lm.Port == 0 || lm.Backend != "cuda" || lm.VRAMUsedMB != 1524 || lm.ContextLength != 4096 || lm.RAMUsedMB != 321 {
		t.Fatalf("%+v", lm)
	}
	args := strings.Join(fs.started[0].args, " ")
	for _, want := range []string{"-m " + filepath.Join(data, "models", "m.gguf"), "--host 127.0.0.1", "--port " + strconv.Itoa(lm.Port), "--alias m", "-c 4096", "-t 6", "--no-webui", "-lv 4"} {
		if !strings.Contains(args, want) {
			t.Errorf("args %q missing %q", args, want)
		}
	}
	if strings.Contains(args, "--device none") {
		t.Error("GPU backend must not disable devices")
	}
	// Loading again returns the running instance.
	if again, err := l.Load(ctx, "m", LoadOptions{}); err != nil || again.PID != lm.PID || len(fs.started) != 1 {
		t.Fatalf("reload: %v", err)
	}
	loaded, _ := l.Loaded(ctx)
	if len(loaded) != 1 || loaded[0].Name != "m" {
		t.Fatalf("%+v", loaded)
	}
	if b, err := os.ReadFile(filepath.Join(data, "logs", "llama-server-m.log")); err != nil || !strings.Contains(string(b), "CUDA0") {
		t.Fatalf("log file: %v", err)
	}
	if err := l.Unload(ctx, "m"); err != nil {
		t.Fatal(err)
	}
	if !fs.started[0].stopped.Load() {
		t.Fatal("process not stopped")
	}
	if err := l.Unload(ctx, "m"); !errors.Is(err, ErrNotLoaded) {
		t.Fatalf("second unload: %v", err)
	}
	if _, err := l.Load(ctx, "nope", LoadOptions{}); !errors.Is(err, ErrUnknownModel) {
		t.Fatalf("unknown: %v", err)
	}
}

func TestLoadFallsBackToVRAMDeltaAndCPUArgs(t *testing.T) {
	g := &fixedGPU{}
	g.used.Store(500)
	fs := &fakeStarter{log: "no buffer lines\n", onStart: func() { g.used.Store(1700) }}
	l, data := newTestServer(t, "cpu", fs, func(o *LlamaServerOptions) { o.GPU = g; o.Threads = 0 })
	writeModel(t, filepath.Join(data, "models"), "m.gguf", 10)
	lm, err := l.Load(context.Background(), "m", LoadOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lm.ContextLength != 8192 {
		t.Fatalf("default ctx %d", lm.ContextLength)
	}
	args := strings.Join(fs.started[0].args, " ")
	if !strings.Contains(args, "--device none") || strings.Contains(args, " -t ") {
		t.Fatalf("args %q", args)
	}
	if lm.VRAMUsedMB != 1200 {
		t.Fatalf("delta %d", lm.VRAMUsedMB)
	}
}

func TestLoadFailures(t *testing.T) {
	fs := &fakeStarter{neverUp: true, log: "error: out of memory\n"}
	l, data := newTestServer(t, "cuda", fs, func(o *LlamaServerOptions) { o.ReadyTimeout = 100 * time.Millisecond })
	writeModel(t, filepath.Join(data, "models"), "m.gguf", 10)
	_, err := l.Load(context.Background(), "m", LoadOptions{})
	if err == nil || !strings.Contains(err.Error(), "not ready") || !strings.Contains(err.Error(), "out of memory") {
		t.Fatalf("got %v", err)
	}
	if !fs.started[0].stopped.Load() {
		t.Fatal("hung process not stopped")
	}
	if loaded, _ := l.Loaded(context.Background()); len(loaded) != 0 {
		t.Fatal("failed load listed")
	}
	fs.failStart = true
	if _, err := l.Load(context.Background(), "m", LoadOptions{}); err == nil || !strings.Contains(err.Error(), "exec format") {
		t.Fatalf("got %v", err)
	}
}

func TestCrashIsReportedAndUnloadAll(t *testing.T) {
	fs := &fakeStarter{}
	l, data := newTestServer(t, "vulkan", fs)
	writeModel(t, filepath.Join(data, "models"), "a.gguf", 10)
	writeModel(t, filepath.Join(data, "models"), "b.gguf", 10)
	events := make(chan map[string]any, 4)
	l.SetNotify(func(kind string, d map[string]any) {
		if kind == "model_unloaded" {
			events <- d
		}
	})
	ctx := context.Background()
	for _, m := range []string{"a", "b"} {
		if _, err := l.Load(ctx, m, LoadOptions{}); err != nil {
			t.Fatal(err)
		}
	}
	fs.started[0].exit(errors.New("exit status 139"))
	select {
	case d := <-events:
		if d["model"] != "a" || d["reason"] != "exited" {
			t.Fatalf("%v", d)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("crash not reported")
	}
	unloaded, err := l.UnloadAll(ctx)
	if err != nil || len(unloaded) != 1 || unloaded[0].Name != "b" {
		t.Fatalf("%+v %v", unloaded, err)
	}
	select {
	case d := <-events:
		t.Fatalf("intentional unload reported as crash: %v", d)
	case <-time.After(50 * time.Millisecond):
	}
}

func TestNotReadyWhileInstalling(t *testing.T) {
	blocked := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { <-blocked }))
	t.Cleanup(func() { close(blocked); srv.Close() })
	data := t.TempDir()
	l := NewLlamaServer(LlamaServerOptions{
		DataDir: data, Backend: "cpu", Release: llamacpp.Pinned().Default,
		Installer: &llamacpp.Installer{Root: filepath.Join(data, "runtime"), Manifest: llamacpp.Pinned(), BaseURL: srv.URL, GOOS: "linux"},
	})
	deadline := time.Now().Add(2 * time.Second)
	for {
		err := l.Health(context.Background())
		if err == nil {
			t.Fatal("healthy without runtime")
		}
		if strings.Contains(err.Error(), "installing llama.cpp") || time.Now().After(deadline) {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	if _, err := l.Load(context.Background(), "m", LoadOptions{}); err == nil || !strings.Contains(err.Error(), "installing") {
		t.Fatalf("load while installing: %v", err)
	}
	if ri := l.RuntimeInfo(); ri.Backend != "cpu" || ri.Release == "" {
		t.Fatalf("%+v", ri)
	}
	l.Close(context.Background())
	if l.Health(context.Background()) == nil {
		t.Fatal("closed runner healthy")
	}
}

func TestDownloadAndDelete(t *testing.T) {
	content := []byte("GGUF model bytes")
	sum := sha256.Sum256(content)
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/models/org/repo/paths-info/main", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `[{"type":"file","path":"Model-Q4_K_M.gguf","size":%d,"lfs":{"oid":%q,"size":%d}}]`, len(content), hex.EncodeToString(sum[:]), len(content))
	})
	mux.HandleFunc("GET /org/repo/resolve/main/Model-Q4_K_M.gguf", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(content) })
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	extra := t.TempDir()
	writeModel(t, extra, "shared.gguf", 3)
	l, data := newTestServer(t, "cpu", &fakeStarter{}, func(o *LlamaServerOptions) {
		o.HF = &hf.Client{BaseURL: srv.URL}
		o.ModelDirs = []string{extra}
	})
	ctx := context.Background()
	var progress [][2]int64
	m, err := l.Download(ctx, DownloadRequest{Repo: "org/repo", File: "Model-Q4_K_M.gguf", Progress: func(d, t int64) { progress = append(progress, [2]int64{d, t}) }})
	if err != nil {
		t.Fatal(err)
	}
	if m.Name != "Model-Q4_K_M" || m.SizeBytes != int64(len(content)) || len(progress) == 0 {
		t.Fatalf("%+v %v", m, progress)
	}
	if _, err := os.Stat(filepath.Join(data, "models", "Model-Q4_K_M.gguf")); err != nil {
		t.Fatal(err)
	}
	if _, err := l.Download(ctx, DownloadRequest{Repo: "org/repo", File: "Model-Q4_K_M.gguf"}); !errors.Is(err, ErrModelExists) {
		t.Fatalf("dup: %v", err)
	}
	if _, err := l.Download(ctx, DownloadRequest{Repo: "org/repo", File: "x.gguf", TargetName: "../evil"}); err == nil {
		t.Fatal("bad name accepted")
	}
	// Loaded models are stopped before deleting.
	if _, err := l.Load(ctx, "Model-Q4_K_M", LoadOptions{}); err != nil {
		t.Fatal(err)
	}
	if err := l.Delete(ctx, "Model-Q4_K_M"); err != nil {
		t.Fatal(err)
	}
	models, _ := l.ListModels(ctx)
	if len(models) != 1 || models[0].Name != "shared" {
		t.Fatalf("%+v", models)
	}
	if err := l.Delete(ctx, "shared"); err == nil || !strings.Contains(err.Error(), "only deletes its own") {
		t.Fatalf("deleted a model_dirs file: %v", err)
	}
}

func TestBenchTargetAndHelpers(t *testing.T) {
	l, data := newTestServer(t, "vulkan", &fakeStarter{})
	writeModel(t, filepath.Join(data, "models"), "m.gguf", 1)
	bt, err := l.BenchTarget(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if bt.Backend != "vulkan" || !strings.HasSuffix(bt.Bench, "llama-bench") || bt.Threads != 6 || bt.Exec == nil {
		t.Fatalf("%+v", bt)
	}
	if p, err := bt.ModelPath("m"); err != nil || filepath.Base(p) != "m.gguf" {
		t.Fatalf("%s %v", p, err)
	}
	for in, want := range map[string]string{"a.gguf": "a", "dir/B.GGUF": "B"} {
		if got := DownloadName(DownloadRequest{File: in}); got != want {
			t.Errorf("%s -> %s", in, got)
		}
	}
	if DownloadName(DownloadRequest{File: "x.gguf", TargetName: "my.gguf"}) != "my" {
		t.Error("target name")
	}
	if got := lastLines("a\nb\nc\n", 2); got != "b\nc" {
		t.Errorf("%q", got)
	}
	r := &ringBuffer{max: 4}
	_, _ = r.Write([]byte("abcdef"))
	if r.String() != "cdef" {
		t.Errorf("ring %q", r.String())
	}
	if !slices.Contains([]int{parseNCtx(cudaLog)}, 4096) || deviceBuffersMB(cudaLog) != 1524 {
		t.Error("log parsing")
	}
}

// --- E2-05 A: fit, backends per model, memory guard ----------------------------------------

type fixedSys struct{ total, used int64 }

func (s fixedSys) Info(context.Context) (sysinfo.Info, error) {
	return sysinfo.Info{RAMTotalMB: s.total, RAMUsedMB: s.used}, nil
}

type gpuList []gpu.GPU

func (g gpuList) GPUs(context.Context) ([]gpu.GPU, error) { return g, nil }

// sparseModel creates a model file of the given size without writing it.
func sparseModel(t *testing.T, dir, name string, size int64) {
	t.Helper()
	_ = os.MkdirAll(dir, 0o755)
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Truncate(p, size); err != nil {
		t.Skipf("sparse files not supported here: %v", err)
	}
}

func argOf(args []string, flag string) (string, bool) {
	for i, a := range args {
		if a == flag && i+1 < len(args) {
			return args[i+1], true
		}
	}
	return "", false
}

func TestGPULoadsFitToDeviceMemoryAndAFixedNumberOfLayersWins(t *testing.T) {
	fs := &fakeStarter{log: cudaLog}
	zero, twenty := 0, 20
	l, data := newTestServer(t, "vulkan", fs, func(o *LlamaServerOptions) {
		o.ModelBackends = map[string]ModelBackend{"pinned": {GPULayers: &twenty}, "cpu-side": {GPULayers: &zero}}
	})
	for _, n := range []string{"auto", "pinned", "cpu-side"} {
		writeModel(t, filepath.Join(data, "models"), n+".gguf", 10)
		if _, err := l.Load(context.Background(), n, LoadOptions{ContextLength: 4096}); err != nil {
			t.Fatal(err)
		}
	}
	byModel := map[string][]string{}
	for _, f := range fs.started {
		alias, _ := argOf(f.args, "--alias")
		byModel[alias] = f.args
	}
	// llama.cpp fits as many layers as the memory holds: no -ngl, --fit on
	if v, ok := argOf(byModel["auto"], "--fit"); !ok || v != "on" || slices.Contains(byModel["auto"], "-ngl") {
		t.Errorf("auto: %v", byModel["auto"])
	}
	if v, _ := argOf(byModel["pinned"], "-ngl"); v != "20" || slices.Contains(byModel["pinned"], "--fit") {
		t.Errorf("pinned: %v", byModel["pinned"])
	}
	if v, _ := argOf(byModel["cpu-side"], "-ngl"); v != "0" {
		t.Errorf("cpu-side: %v", byModel["cpu-side"])
	}
}

func TestModelsOfDifferentBackendsRunSideBySide(t *testing.T) {
	// A 7B on Vulkan and a 30B on the CPU build (both runtimes installed, one process each).
	fs := &fakeStarter{}
	data := t.TempDir()
	in := installed(t, data, "vulkan")
	_ = installed(t, data, "cpu") // second runtime in the same runtime root
	l := NewLlamaServer(LlamaServerOptions{
		DataDir: data, Backend: "vulkan", Release: "b1", Installer: in, Start: fs.start,
		PollInterval: 5 * time.Millisecond, ReadyTimeout: 5 * time.Second,
		ModelBackends: map[string]ModelBackend{"big": {Backend: "cpu"}},
	})
	t.Cleanup(func() { l.Close(context.Background()) })
	waitHealthy(t, l)
	writeModel(t, filepath.Join(data, "models"), "small.gguf", 10)
	writeModel(t, filepath.Join(data, "models"), "big.gguf", 10)
	deadline := time.Now().Add(5 * time.Second)
	var lastErr error
	for {
		if _, lastErr = l.installationFor("cpu"); lastErr == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("cpu runtime never installed: %v", lastErr)
		}
		time.Sleep(5 * time.Millisecond)
	}
	small, err := l.Load(context.Background(), "small", LoadOptions{})
	if err != nil {
		t.Fatal(err)
	}
	big, err := l.Load(context.Background(), "big", LoadOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if small.Backend != "vulkan" || big.Backend != "cpu" {
		t.Fatalf("%+v %+v", small, big)
	}
	binaries := map[string]string{}
	for _, f := range fs.started {
		alias, _ := argOf(f.args[1:], "--alias")
		binaries[alias] = f.args[0]
	}
	if !strings.Contains(binaries["small"], "b1-vulkan") || !strings.Contains(binaries["big"], "b1-cpu") {
		t.Fatalf("binaries %v", binaries)
	}
	if !slices.Contains(fs.started[1].args, "none") { // --device none for the CPU build
		t.Fatalf("cpu model must not use a device: %v", fs.started[1].args)
	}
	loaded, _ := l.Loaded(context.Background())
	if len(loaded) != 2 {
		t.Fatalf("both must be loaded: %+v", loaded)
	}
	bt, err := l.BenchTarget(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	mb, err := bt.For("big")
	if err != nil || mb.Backend != "cpu" || !strings.Contains(mb.Bench, "b1-cpu") {
		t.Fatalf("%+v %v", mb, err)
	}
	if mb, _ := bt.For("small"); mb.Backend != "vulkan" || !strings.Contains(mb.Bench, "b1-vulkan") {
		t.Fatalf("%+v", mb)
	}
}

func TestAModelWhoseBackendIsNotInstalledYetSaysSo(t *testing.T) {
	fs := &fakeStarter{}
	data := t.TempDir()
	in := installed(t, data, "vulkan") // no cpu runtime and nothing to download it from
	in.Manifest = llamacpp.Manifest{Default: "b1", Releases: map[string]llamacpp.Release{"b1": {}}}
	l := NewLlamaServer(LlamaServerOptions{
		DataDir: data, Backend: "vulkan", Release: "b1", Installer: in, Start: fs.start,
		PollInterval: 5 * time.Millisecond, RetryInterval: time.Hour,
		ModelBackends: map[string]ModelBackend{"big": {Backend: "cpu"}},
	})
	t.Cleanup(func() { l.Close(context.Background()) })
	waitHealthy(t, l) // the node backend is fine; it stays available
	writeModel(t, filepath.Join(data, "models"), "big.gguf", 10)
	time.Sleep(50 * time.Millisecond)
	if _, err := l.Load(context.Background(), "big", LoadOptions{}); err == nil || !strings.Contains(err.Error(), "cpu") {
		t.Fatalf("got %v", err)
	}
}

func TestMemoryGuardRefusesWarnsOrIsOff(t *testing.T) {
	big := int64(19 << 30) // a 30B-class model; the file is sparse
	newLoaded := func(t *testing.T, sys fixedSys, mutate func(*LlamaServerOptions)) (*LlamaServer, *fakeStarter) {
		fs := &fakeStarter{}
		l, data := newTestServer(t, "cpu", fs, func(o *LlamaServerOptions) {
			o.SysInfo = sys
			if mutate != nil {
				mutate(o)
			}
		})
		sparseModel(t, filepath.Join(data, "models"), "big.gguf", big)
		return l, fs
	}
	// 32 GB laptop with 14 GB in use: 18 GB free, the model needs ~20 GB
	l, fs := newLoaded(t, fixedSys{total: 32768, used: 14336}, nil)
	_, err := l.Load(context.Background(), "big", LoadOptions{ContextLength: 8192})
	if !errors.Is(err, ErrInsufficientMemory) || !strings.Contains(err.Error(), "not enough free RAM") {
		t.Fatalf("got %v", err)
	}
	if len(fs.started) != 0 {
		t.Fatal("a refused load must not start a process")
	}

	// warn: it loads anyway and reports the shortfall
	var events []map[string]any
	l, fs = newLoaded(t, fixedSys{total: 32768, used: 14336}, func(o *LlamaServerOptions) { o.GuardMode = "warn" })
	l.SetNotify(func(kind string, data map[string]any) {
		if kind == "error" {
			events = append(events, data)
		}
	})
	if _, err := l.Load(context.Background(), "big", LoadOptions{ContextLength: 8192}); err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 || events[0]["code"] != "insufficient_memory" || len(fs.started) != 1 {
		t.Fatalf("%v %d", events, len(fs.started))
	}

	// off, and enough room
	l, _ = newLoaded(t, fixedSys{total: 32768, used: 14336}, func(o *LlamaServerOptions) { o.GuardMode = "off" })
	if _, err := l.Load(context.Background(), "big", LoadOptions{}); err != nil {
		t.Fatal(err)
	}
	l, _ = newLoaded(t, fixedSys{total: 65536, used: 8000}, nil)
	if _, err := l.Load(context.Background(), "big", LoadOptions{}); err != nil {
		t.Fatalf("64 GB machine: %v", err)
	}
	// a reserve the config raises makes the same load fit no more
	l, _ = newLoaded(t, fixedSys{total: 65536, used: 8000}, func(o *LlamaServerOptions) { o.GuardReserveMB = 50000 })
	if _, err := l.Load(context.Background(), "big", LoadOptions{}); !errors.Is(err, ErrInsufficientMemory) {
		t.Fatalf("reserve ignored: %v", err)
	}
}

func TestMemoryGuardCountsAnIntegratedGPUAsSystemMemory(t *testing.T) {
	igpu := gpuList{{Index: 0, Name: "Iris Xe", Vendor: "intel", Integrated: true, VRAMTotalMB: 128, SharedTotalMB: 16384}}
	dgpu := gpuList{{Index: 0, Name: "RTX", Vendor: "nvidia", VRAMTotalMB: 8192, VRAMUsedMB: 500}}
	load := func(backend string, gpus gpuList) error {
		fs := &fakeStarter{}
		l, data := newTestServer(t, backend, fs, func(o *LlamaServerOptions) {
			o.GPU, o.SysInfo = gpus, fixedSys{total: 32768, used: 14336}
		})
		sparseModel(t, filepath.Join(data, "models"), "big.gguf", 19<<30)
		_, err := l.Load(context.Background(), "big", LoadOptions{ContextLength: 8192})
		return err
	}
	if err := load("vulkan", igpu); !errors.Is(err, ErrInsufficientMemory) {
		t.Fatalf("an iGPU offload is system memory: %v", err)
	}
	// on a discrete GPU with 7.6 GB free most of the model does not fit either, but only
	// what does not fit lands in RAM: 20 - 7.6 GB fits into 18 GB free
	if err := load("cuda", dgpu); err != nil {
		t.Fatalf("a discrete GPU takes its share: %v", err)
	}
}

func hfServer(t *testing.T, size int64) *hf.Client {
	t.Helper()
	sum := sha256.Sum256([]byte("x"))
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/models/org/repo/paths-info/main", func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintf(w, `[{"type":"file","path":"Big-Q4_K_M.gguf","size":%d,"lfs":{"oid":%q,"size":%d}}]`, size, hex.EncodeToString(sum[:]), size)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return &hf.Client{BaseURL: srv.URL}
}

func TestDownloadsThatDoNotFitAreRefused(t *testing.T) {
	const gb = int64(1 << 30)
	free := 20 * gb
	l, data := newTestServer(t, "cpu", &fakeStarter{}, func(o *LlamaServerOptions) {
		o.HF = hfServer(t, 19*gb)
		o.DiskFree = func(string) (diskfree.Usage, error) {
			return diskfree.Usage{TotalBytes: 100 * gb, FreeBytes: free}, nil
		}
	})
	req := DownloadRequest{Repo: "org/repo", File: "Big-Q4_K_M.gguf"}
	ctx := context.Background()
	// 19 GB + the 1 GB reserve into 20 GB free: exactly fits
	if err := l.CheckDownload(ctx, req); err != nil {
		t.Fatalf("fits: %v", err)
	}
	free = 19*gb + gb/2 // 19.5 GB free: not enough for 19 GB + 1 GB
	err := l.CheckDownload(ctx, req)
	if !errors.Is(err, ErrInsufficientDisk) || !strings.Contains(err.Error(), "needs 19456 MB") {
		t.Fatalf("got %v", err)
	}
	if _, err := l.Download(ctx, req); !errors.Is(err, ErrInsufficientDisk) {
		t.Fatalf("Download must refuse, too: %v", err)
	}
	if _, err := os.Stat(filepath.Join(data, "models", "Big-Q4_K_M.gguf.part")); err == nil {
		t.Fatal("a refused download must not start")
	}
	// a partial file from an earlier attempt is not counted again
	if err := os.MkdirAll(filepath.Join(data, "models"), 0o755); err != nil {
		t.Fatal(err)
	}
	part := filepath.Join(data, "models", "Big-Q4_K_M.gguf.part")
	_ = os.WriteFile(part, nil, 0o644)
	if err := os.Truncate(part, 5*gb); err != nil {
		t.Skip("sparse files not supported")
	}
	if err := l.CheckDownload(ctx, req); err != nil {
		t.Fatalf("resume of a 5 GB partial file needs only 14 GB: %v", err)
	}
	if l.ModelDir() != filepath.Join(data, "models") {
		t.Fatalf("model dir %s", l.ModelDir())
	}
}

func TestACheckThatCannotBeMadeDoesNotBlock(t *testing.T) {
	l, _ := newTestServer(t, "cpu", &fakeStarter{}, func(o *LlamaServerOptions) {
		o.HF = &hf.Client{BaseURL: "http://127.0.0.1:1"} // unreachable
	})
	err := l.CheckDownload(context.Background(), DownloadRequest{Repo: "org/repo", File: "x.gguf"})
	if err == nil || errors.Is(err, ErrInsufficientDisk) {
		t.Fatalf("an unreachable Hugging Face is an error, but not 'insufficient disk': %v", err)
	}
}

// iris is the iGPU of the low-power laptop: 16 GB of shared memory that the driver reports as free.
var iris = gpuList{{Index: 0, Name: "Iris Xe", Vendor: "intel", Integrated: true, VRAMTotalMB: 128, SharedTotalMB: 16384}}

// ggufModel writes a file with a real GGUF header (llama architecture, 32 blocks) so the runner
// knows the layer count.
func ggufModel(t *testing.T, dir, name string) {
	t.Helper()
	var b bytes.Buffer
	w := func(v any) { _ = binary.Write(&b, binary.LittleEndian, v) }
	str := func(s string) { w(uint64(len(s))); b.WriteString(s) }
	b.WriteString("GGUF")
	w(uint32(3))
	w(uint64(0))
	w(uint64(5))
	str("general.architecture")
	w(uint32(8)) // string
	str("llama")
	for _, kv := range []struct {
		k string
		v uint32
	}{{"llama.embedding_length", 4096}, {"llama.block_count", 32}, {"llama.attention.head_count", 32}, {"llama.attention.head_count_kv", 8}} {
		str(kv.k)
		w(uint32(4)) // uint32
		w(kv.v)
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name), b.Bytes(), 0o644); err != nil {
		t.Fatal(err)
	}
}

var dgpu = gpuList{{Index: 0, Name: "RTX", Vendor: "nvidia", VRAMTotalMB: 8192}}

func TestTheIGPUSafetyMarginIsAQuarterOfTheSharedMemoryAtLeast4GB(t *testing.T) {
	if got := IntegratedFitMargin(iris); got != 4096 {
		t.Fatalf("16 GB shared memory: margin %d, want the 4096 floor", got)
	}
	big := []gpu.GPU{{Integrated: true, SharedTotalMB: 64 << 10}}
	if got := IntegratedFitMargin(big); got != 16384 {
		t.Fatalf("64 GB shared: margin %d, want a quarter", got)
	}
	if got := IntegratedFitMargin([]gpu.GPU{{VRAMTotalMB: 8192}}); got != 0 {
		t.Fatalf("a discrete GPU keeps llama.cpp's default: %d", got)
	}
}

func loadArgs(t *testing.T, gpus gpuList, size int64, mutate func(*LlamaServerOptions)) [][]string {
	t.Helper()
	fs := &fakeStarter{}
	l, data := newTestServer(t, "vulkan", fs, func(o *LlamaServerOptions) {
		o.GPU = gpus
		if mutate != nil {
			mutate(o)
		}
	})
	sparseModel(t, filepath.Join(data, "models"), "m.gguf", size)
	if _, err := l.Load(context.Background(), "m", LoadOptions{ContextLength: 8192}); err != nil {
		t.Fatal(err)
	}
	var all [][]string
	for _, f := range fs.started {
		all = append(all, f.args)
	}
	return all
}

func TestAnIntegratedGPUGetsTheWholeModelOrNone(t *testing.T) {
	// 16 GB shared, 4 GB safety margin: 12 GB may be used
	small := loadArgs(t, iris, 5<<30, nil)
	if len(small) != 1 {
		t.Fatalf("%d starts", len(small))
	}
	if v, _ := argOf(small[0], "-ngl"); v != "all" {
		t.Errorf("a model that fits goes fully to the iGPU: %v", small[0])
	}
	if v, _ := argOf(small[0], "--fit"); v != "off" || slices.Contains(small[0], "--fit-target") {
		t.Errorf("no fit (it would offload partly): %v", small[0])
	}
	// what does not fit goes to the CPU at once: one start, no failed attempt first
	big := loadArgs(t, iris, 13<<30, nil)
	if len(big) != 1 || !slices.Contains(big[0], "none") || slices.Contains(big[0], "-ngl") {
		t.Fatalf("a model larger than the budget must start on the CPU directly: %v", big)
	}
	// the margin is configurable: with 2 GB the 13 GB model fits the 14 GB that are left
	custom := loadArgs(t, iris, 13<<30, func(o *LlamaServerOptions) { o.FitMarginMB = 2048 })
	if v, _ := argOf(custom[0], "-ngl"); len(custom) != 1 || v != "all" {
		t.Fatalf("configured margin ignored: %v", custom)
	}
}

func TestTheIGPUBudgetCountsWhatModelsOnItAlreadyHold(t *testing.T) {
	fs := &fakeStarter{log: cudaLog}
	l, data := newTestServer(t, "vulkan", fs, func(o *LlamaServerOptions) { o.GPU = iris })
	sparseModel(t, filepath.Join(data, "models"), "first.gguf", 5<<30)
	sparseModel(t, filepath.Join(data, "models"), "second.gguf", 5<<30)
	for _, n := range []string{"first", "second"} {
		if _, err := l.Load(context.Background(), n, LoadOptions{ContextLength: 8192}); err != nil {
			t.Fatal(err)
		}
	}
	// the first holds ~1.2 GB by its log (cudaLog) plus nothing else: both fit; but a second
	// 5 GB model next to a 5 GB one that the driver counts as held must not be planned blindly
	l.mu.Lock()
	l.loaded["first"].lm.VRAMUsedMB = 9000
	l.mu.Unlock()
	sparseModel(t, filepath.Join(data, "models"), "third.gguf", 5<<30)
	if _, err := l.Load(context.Background(), "third", LoadOptions{ContextLength: 8192}); err != nil {
		t.Fatal(err)
	}
	last := fs.started[len(fs.started)-1].args
	if !slices.Contains(last, "none") {
		t.Fatalf("12 GB budget − 9 GB already held leaves no room: the model starts on the CPU: %v", last)
	}
}

func TestADiscreteGPUStillFitsThenHalfThenCPU(t *testing.T) {
	// the first two starts die of an allocation failure; the third runs on the CPU
	fs := &fakeStarter{failWhen: func(args []string) string {
		if slices.Contains(args, "none") {
			return ""
		}
		return "ggml_vulkan: Device memory allocation of size 805306368 failed\nllama_kv_cache: failed to allocate Vulkan0 buffer of size 805306368\n"
	}}
	l, data := newTestServer(t, "vulkan", fs, func(o *LlamaServerOptions) { o.GPU = dgpu })
	ggufModel(t, filepath.Join(data, "models"), "m.gguf")
	var mu sync.Mutex
	var events []map[string]any
	l.SetNotify(func(kind string, d map[string]any) {
		if kind == "model_load_retry" {
			mu.Lock()
			events = append(events, d)
			mu.Unlock()
		}
	})
	lm, err := l.Load(context.Background(), "m", LoadOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lm.Backend != "cpu" {
		t.Errorf("the model runs without a device, so it reports cpu: %+v", lm)
	}
	if len(fs.started) != 3 {
		t.Fatalf("%d starts, want fit / half / cpu", len(fs.started))
	}
	if v, _ := argOf(fs.started[0].args, "--fit"); v != "on" {
		t.Errorf("first attempt: %v", fs.started[0].args)
	}
	if v, ok := argOf(fs.started[1].args, "-ngl"); !ok || v == "" {
		t.Errorf("second attempt has a fixed, smaller layer count: %v", fs.started[1].args)
	}
	if !slices.Contains(fs.started[2].args, "none") {
		t.Errorf("third attempt is CPU only: %v", fs.started[2].args)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(events) != 2 || events[0]["attempt"] != 2 || events[1]["plan"] != "cpu" || !strings.Contains(events[0]["error"].(string), "failed to allocate") {
		t.Errorf("events %v", events)
	}
}

func TestAnIGPUFailureCostsOneRetryOnTheCPU(t *testing.T) {
	fs := &fakeStarter{failWhen: func(args []string) string {
		if slices.Contains(args, "none") {
			return ""
		}
		return "llama_kv_cache: failed to allocate Vulkan0 buffer of size 805306368\n"
	}}
	l, data := newTestServer(t, "vulkan", fs, func(o *LlamaServerOptions) { o.GPU = iris })
	writeModel(t, filepath.Join(data, "models"), "m.gguf", 10)
	var retries []map[string]any
	l.SetNotify(func(kind string, d map[string]any) {
		if kind == "model_load_retry" {
			retries = append(retries, d)
		}
	})
	lm, err := l.Load(context.Background(), "m", LoadOptions{})
	if err != nil || lm.Backend != "cpu" {
		t.Fatalf("%+v %v", lm, err)
	}
	if len(fs.started) != 2 || len(retries) != 1 || retries[0]["plan"] != "cpu" {
		t.Fatalf("%d starts, %v", len(fs.started), retries)
	}
}

func TestTheNoHostOptionIsPassedOnRequest(t *testing.T) {
	twenty := 20
	args := loadArgs(t, iris, 5<<30, func(o *LlamaServerOptions) {
		o.ModelBackends = map[string]ModelBackend{"m": {GPULayers: &twenty, NoHost: true}}
	})
	if !slices.Contains(args[0], "--no-host") {
		t.Errorf("%v", args[0])
	}
	plain := loadArgs(t, iris, 5<<30, nil)
	if slices.Contains(plain[0], "--no-host") {
		t.Errorf("no-host is an opt-in: %v", plain[0])
	}
}

func TestLoadDoesNotRetryOtherFailuresOrFixedLayers(t *testing.T) {
	notOOM := &fakeStarter{failWhen: func([]string) string { return "error: unknown model architecture\n" }}
	l, data := newTestServer(t, "vulkan", notOOM, func(o *LlamaServerOptions) { o.GPU = iris })
	writeModel(t, filepath.Join(data, "models"), "m.gguf", 10)
	if _, err := l.Load(context.Background(), "m", LoadOptions{}); err == nil {
		t.Fatal("must fail")
	}
	if len(notOOM.started) != 1 {
		t.Errorf("%d starts for a failure that is not about memory", len(notOOM.started))
	}

	ten := 10
	oom := &fakeStarter{failWhen: func([]string) string { return "failed to allocate Vulkan0 buffer\n" }}
	l, data = newTestServer(t, "vulkan", oom, func(o *LlamaServerOptions) {
		o.GPU, o.ModelBackends = iris, map[string]ModelBackend{"m": {GPULayers: &ten}}
	})
	writeModel(t, filepath.Join(data, "models"), "m.gguf", 10)
	if _, err := l.Load(context.Background(), "m", LoadOptions{}); err == nil {
		t.Fatal("must fail")
	}
	if len(oom.started) != 1 {
		t.Errorf("a configured layer count is respected, no retry: %d starts", len(oom.started))
	}
}

func TestServerStartsWithItsOwnKeyAndRestrictedCORS(t *testing.T) {
	fs := &fakeStarter{}
	l, data := newTestServer(t, "cpu", fs)
	for _, n := range []string{"a", "b"} {
		writeModel(t, filepath.Join(data, "models"), n+".gguf", 10)
		if _, err := l.Load(context.Background(), n, LoadOptions{}); err != nil {
			t.Fatal(err)
		}
	}
	a, b := fs.started[0], fs.started[1]
	if len(a.key) != 64 || a.key == b.key {
		t.Errorf("every start gets its own 256-bit key: %q %q", a.key, b.key)
	}
	for _, f := range fs.started {
		if slices.Contains(f.args, f.key) {
			t.Errorf("the key must not appear on the command line: %v", f.args)
		}
		if v, _ := argOf(f.args, "--cors-origins"); v != "localhost" || !slices.Contains(f.args, "--no-cors-credentials") {
			t.Errorf("CORS: %v", f.args)
		}
		if v, _ := argOf(f.args, "--host"); v != "127.0.0.1" {
			t.Errorf("host %q", v)
		}
	}
	if left, _ := filepath.Glob(filepath.Join(data, "run", "*.key")); len(left) != 0 {
		t.Errorf("key files stay on disk: %v", left)
	}
	pt, err := l.ProxyTarget(context.Background(), "a")
	if err != nil || pt.APIKey != a.key {
		t.Errorf("the proxy target carries the key of that start: %+v %v", pt, err)
	}
}

func TestLoadedRAMIsThePeakNotTheLatestSample(t *testing.T) {
	in := &instance{}
	for _, c := range []struct{ sample, want int64 }{{5177, 5177}, {33, 5177}, {4000, 5177}, {6000, 6000}} {
		if got := in.peakRAM(c.sample); got != c.want {
			t.Errorf("sample %d: %d, want %d", c.sample, got, c.want)
		}
	}
}

func TestAWaitingLoadCanBeCanceledAndADuplicateReturnsTheLoadedModel(t *testing.T) {
	fs := &fakeStarter{}
	l, data := newTestServer(t, "cpu", fs)
	writeModel(t, filepath.Join(data, "models"), "m.gguf", 10)
	var wg sync.WaitGroup
	results := make([]LoadedModel, 3)
	for i := range results {
		wg.Add(1)
		go func() {
			defer wg.Done()
			lm, err := l.Load(context.Background(), "m", LoadOptions{})
			if err != nil {
				t.Error(err)
			}
			results[i] = lm
		}()
	}
	wg.Wait()
	if len(fs.started) != 1 {
		t.Fatalf("three concurrent loads of one model start %d processes", len(fs.started))
	}
	if results[0].PID == 0 || results[0] != results[1] || results[1] != results[2] {
		t.Errorf("all callers get the same model: %+v", results)
	}

	// a load waiting for the slot ends with the context
	l.loadSem <- struct{}{}
	ctx, cancel := context.WithCancel(context.Background())
	writeModel(t, filepath.Join(data, "models"), "n.gguf", 10)
	done := make(chan error, 1)
	go func() { _, err := l.Load(ctx, "n", LoadOptions{}); done <- err }()
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Errorf("got %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("a canceled load must not wait for the slot")
	}
	<-l.loadSem
}

func TestACanceledLoadStopsTheStartingServer(t *testing.T) {
	fs := &fakeStarter{neverUp: true}
	l, data := newTestServer(t, "cpu", fs)
	writeModel(t, filepath.Join(data, "models"), "m.gguf", 10)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { _, err := l.Load(ctx, "m", LoadOptions{}); done <- err }()
	for range 200 {
		fs.mu.Lock()
		n := len(fs.started)
		fs.mu.Unlock()
		if n > 0 {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("got %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("cancel did not end the load")
	}
	if !fs.started[0].killed.Load() {
		t.Error("a canceled load kills the half-started server at once (it does not react to a polite request)")
	}
	if loaded, _ := l.Loaded(context.Background()); len(loaded) != 0 {
		t.Errorf("nothing is loaded: %+v", loaded)
	}
}

func TestFreezeSuspendsEveryModelProcessAndThawContinuesThem(t *testing.T) {
	fs := &fakeStarter{}
	l, data := newTestServer(t, "cpu", fs)
	for _, n := range []string{"a", "b"} {
		writeModel(t, filepath.Join(data, "models"), n+".gguf", 10)
		if _, err := l.Load(context.Background(), n, LoadOptions{}); err != nil {
			t.Fatal(err)
		}
	}
	var _ Freezer = l
	if err := l.Freeze(context.Background()); err != nil {
		t.Fatal(err)
	}
	for i, f := range fs.started {
		if !f.suspended.Load() {
			t.Errorf("model %d is not frozen", i)
		}
	}
	if err := l.Thaw(context.Background()); err != nil {
		t.Fatal(err)
	}
	for i, f := range fs.started {
		if f.suspended.Load() {
			t.Errorf("model %d is still frozen", i)
		}
	}
	// a model that starts while the node ducks starts frozen; Thaw lets it become ready
	if err := l.Freeze(context.Background()); err != nil {
		t.Fatal(err)
	}
	writeModel(t, filepath.Join(data, "models"), "c.gguf", 10)
	done := make(chan error, 1)
	go func() { _, err := l.Load(context.Background(), "c", LoadOptions{}); done <- err }()
	deadline := time.Now().Add(3 * time.Second)
	for {
		fs.mu.Lock()
		n := len(fs.started)
		fs.mu.Unlock()
		if n == 3 && fs.started[2].suspended.Load() {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the new server was not frozen")
		}
		time.Sleep(5 * time.Millisecond)
	}
	select {
	case err := <-done:
		t.Fatalf("a frozen server cannot be ready: %v", err)
	case <-time.After(150 * time.Millisecond):
	}
	if err := l.Thaw(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestTheReadyTimeoutDoesNotRunWhileFrozen(t *testing.T) {
	fs := &fakeStarter{}
	l, data := newTestServer(t, "cpu", fs, func(o *LlamaServerOptions) { o.ReadyTimeout = 200 * time.Millisecond })
	writeModel(t, filepath.Join(data, "models"), "m.gguf", 10)
	_ = l.Freeze(context.Background())
	done := make(chan error, 1)
	go func() { _, err := l.Load(context.Background(), "m", LoadOptions{}); done <- err }()
	select {
	case err := <-done:
		t.Fatalf("the load gave up while the node was frozen: %v", err)
	case <-time.After(700 * time.Millisecond): // several ready timeouts
	}
	_ = l.Thaw(context.Background())
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestTheMemoryGuardLooksAgainBeforeEveryAttempt(t *testing.T) {
	// the first attempt passes the guard and dies of an allocation failure; what it left behind
	// (or another program took meanwhile) makes the retry refuse instead of pushing into swap
	var free atomic.Int64
	free.Store(30000)
	fs := &fakeStarter{failWhen: func(args []string) string {
		if slices.Contains(args, "none") {
			return ""
		}
		free.Store(500) // almost nothing is free after the failed attempt
		return "failed to allocate Vulkan0 buffer\n"
	}}
	l, data := newTestServer(t, "vulkan", fs, func(o *LlamaServerOptions) {
		o.GPU, o.SysInfo = dgpu, dynSys{total: 32768, free: &free}
	})
	ggufModel(t, filepath.Join(data, "models"), "m.gguf")
	_, err := l.Load(context.Background(), "m", LoadOptions{})
	if !errors.Is(err, ErrInsufficientMemory) || !strings.Contains(err.Error(), "after the attempt before failed") {
		t.Fatalf("got %v", err)
	}
	if len(fs.started) != 1 {
		t.Fatalf("%d starts: the guard must stop the second attempt", len(fs.started))
	}
}

// dynSys is a machine whose free RAM the test changes.
type dynSys struct {
	total int64
	free  *atomic.Int64
}

func (s dynSys) Info(context.Context) (sysinfo.Info, error) {
	return sysinfo.Info{RAMTotalMB: s.total, RAMUsedMB: s.total - s.free.Load()}, nil
}

func TestTheNextAttemptWaitsUntilTheFailedOnesMemoryIsFree(t *testing.T) {
	var free atomic.Int64
	free.Store(30000)
	var polls atomic.Int32
	fs := &fakeStarter{failWhen: func(args []string) string {
		if slices.Contains(args, "none") {
			return ""
		}
		free.Store(20000) // the failed server's memory is still being released
		return "failed to allocate Vulkan0 buffer\n"
	}}
	sys := recoveringSys{total: 32768, free: &free, polls: &polls}
	l, data := newTestServer(t, "vulkan", fs, func(o *LlamaServerOptions) { o.GPU, o.SysInfo = dgpu, sys; o.GuardMode = "off" })
	ggufModel(t, filepath.Join(data, "models"), "m.gguf")
	start := time.Now()
	if _, err := l.Load(context.Background(), "m", LoadOptions{}); err != nil {
		t.Fatal(err)
	}
	if time.Since(start) < 300*time.Millisecond {
		t.Errorf("the retry started before the memory came back (%v)", time.Since(start))
	}
}

// recoveringSys reports the free RAM rising back to 30000 MB after a few looks.
type recoveringSys struct {
	total int64
	free  *atomic.Int64
	polls *atomic.Int32
}

func (s recoveringSys) Info(context.Context) (sysinfo.Info, error) {
	if s.free.Load() < 30000 && s.polls.Add(1) > 3 {
		s.free.Store(30000)
	}
	return sysinfo.Info{RAMTotalMB: s.total, RAMUsedMB: s.total - s.free.Load()}, nil
}

func TestAFailedServerThatCannotBeStoppedBlocksTheRetry(t *testing.T) {
	fs := &fakeStarter{
		configure: func(f *fakeServer) { f.stopErr = errors.New("access denied") },
		failWhen:  func([]string) string { return "failed to allocate Vulkan0 buffer\n" },
	}
	l, data := newTestServer(t, "vulkan", fs, func(o *LlamaServerOptions) { o.GPU = dgpu })
	ggufModel(t, filepath.Join(data, "models"), "m.gguf")
	_, err := l.Load(context.Background(), "m", LoadOptions{})
	if !errors.Is(err, errNotStopped) {
		t.Fatalf("got %v", err)
	}
	if len(fs.started) != 1 {
		t.Fatalf("no second server while the first one may still hold its memory: %d starts", len(fs.started))
	}
}

func TestACanceledLoadReturnsQuicklyEvenIfTheSystemStillTearsTheProcessDown(t *testing.T) {
	slow := make(chan struct{})
	fs := &fakeStarter{neverUp: true, configure: func(f *fakeServer) { f.slowDeath = slow }}
	l, data := newTestServer(t, "cpu", fs, func(o *LlamaServerOptions) { o.CancelWait = 50 * time.Millisecond })
	writeModel(t, filepath.Join(data, "models"), "m.gguf", 10)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { _, err := l.Load(ctx, "m", LoadOptions{}); done <- err }()
	for range 200 {
		fs.mu.Lock()
		n := len(fs.started)
		fs.mu.Unlock()
		if n > 0 {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("got %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("a canceled load must not wait for the operating system")
	}
	// the next load waits for the dying server before it starts another one
	fs.neverUp = false
	next := make(chan error, 1)
	go func() { _, err := l.Load(context.Background(), "m", LoadOptions{}); next <- err }()
	select {
	case err := <-next:
		t.Fatalf("started while the killed server was still there: %v", err)
	case <-time.After(300 * time.Millisecond):
	}
	close(slow)
	if err := <-next; err != nil {
		t.Fatal(err)
	}
}

// Slots (-np): the context of a load is the context of ONE request. llama-server, left alone,
// starts 4 slots in one shared window (b11223: n_slots = 4, kv_unified = true), where a long story
// on one slot starves the chat on another ("Context size has been exceeded"); with an explicit
// -np every slot gets its own share of -c (-c 1024 -np 2 -> n_ctx_seq 512, verified with b11223).
func TestLoadGivesEverySlotTheWholeContext(t *testing.T) {
	log := strings.Replace(cudaLog, "llama_context: n_ctx         = 4096", "llama_context: n_ctx         = 8192\nllama_context: n_ctx_seq     = 4096", 1)
	fs := &fakeStarter{log: log}
	l, data := newTestServer(t, "cuda", fs, func(o *LlamaServerOptions) { o.GPU = &fixedGPU{} })
	writeModel(t, filepath.Join(data, "models"), "m.gguf", 10)
	lm, err := l.Load(context.Background(), "m", LoadOptions{ContextLength: 4096, NParallel: 2})
	if err != nil {
		t.Fatal(err)
	}
	args := strings.Join(fs.started[0].args, " ")
	if !strings.Contains(args, "-np 2 -c 8192") {
		t.Errorf("args %q: want -np 2 -c 8192 (context x slots)", args)
	}
	if lm.ContextLength != 4096 || lm.NParallel != 2 {
		t.Errorf("a slot has 4096 tokens, 2 slots: %+v", lm)
	}
}

func TestLoadDefaultsToOneSlot(t *testing.T) {
	fs := &fakeStarter{log: cudaLog}
	l, data := newTestServer(t, "cuda", fs, func(o *LlamaServerOptions) { o.GPU = &fixedGPU{} })
	writeModel(t, filepath.Join(data, "models"), "m.gguf", 10)
	lm, err := l.Load(context.Background(), "m", LoadOptions{ContextLength: 4096})
	if err != nil {
		t.Fatal(err)
	}
	if args := strings.Join(fs.started[0].args, " "); !strings.Contains(args, "-np 1 -c 4096") {
		t.Errorf("args %q", args)
	}
	if lm.NParallel != 1 || lm.ContextLength != 4096 {
		t.Errorf("%+v", lm)
	}
}

func TestLoadPassesJinjaExplicitly(t *testing.T) {
	for _, noJinja := range []bool{false, true} {
		fs := &fakeStarter{log: cudaLog}
		l, data := newTestServer(t, "cuda", fs, func(o *LlamaServerOptions) { o.GPU = &fixedGPU{} })
		writeModel(t, filepath.Join(data, "models"), "m.gguf", 10)
		if _, err := l.Load(context.Background(), "m", LoadOptions{ContextLength: 4096, NoJinja: noJinja}); err != nil {
			t.Fatal(err)
		}
		args := strings.Join(fs.started[0].args, " ")
		want, other := "--jinja", "--no-jinja"
		if noJinja {
			want, other = other, want
		}
		if !strings.Contains(args+" ", " "+want+" ") || strings.Contains(args+" ", " "+other+" ") {
			t.Errorf("noJinja=%v: args %q want %s", noJinja, args, want)
		}
		// the agent tools of llama-server (shell, files) are never switched on
		if strings.Contains(args, "--tools") || strings.Contains(args, "--agent") || strings.Contains(args, "--mcp") {
			t.Errorf("args enable llama-server's own tools: %q", args)
		}
	}
}

func TestParseNCtxSeq(t *testing.T) {
	log := "llama_context: n_ctx                 = 1024\nllama_context: n_ctx_seq             = 512\nllama_context: n_ctx_seq (512) < n_ctx_train (2048) -- x\n"
	if parseNCtx(log) != 1024 || parseNCtxSeq(log) != 512 || parseNCtxSeq("nothing") != 0 {
		t.Fatalf("%d %d", parseNCtx(log), parseNCtxSeq(log))
	}
}

func TestModelFileAndReceiveDir(t *testing.T) {
	extra := t.TempDir()
	writeModel(t, extra, "shared.gguf", 3)
	l, data := newTestServer(t, "cpu", &fakeStarter{}, func(o *LlamaServerOptions) { o.ModelDirs = []string{extra} })
	writeModel(t, filepath.Join(data, "models"), "own.gguf", 5)
	writeModel(t, filepath.Join(data, "models"), "big-00001-of-00002.gguf", 7)
	var _ ModelFiler = l
	var _ ModelReceiver = l

	p, size, own, err := l.ModelFile("own")
	if err != nil || size != 5 || !own || p != filepath.Join(data, "models", "own.gguf") {
		t.Fatalf("own: %q %d %v %v", p, size, own, err)
	}
	if _, size, own, err := l.ModelFile("shared"); err != nil || size != 3 || own {
		t.Fatalf("shared: %d %v %v", size, own, err)
	}
	if _, _, _, err := l.ModelFile("big"); !errors.Is(err, ErrSplitModel) {
		t.Fatalf("split: %v", err)
	}
	for _, bad := range []string{"nope", "../own", "own.gguf"} {
		if _, _, _, err := l.ModelFile(bad); !errors.Is(err, ErrUnknownModel) {
			t.Fatalf("%s: %v", bad, err)
		}
	}
	if l.ReceiveDir() != filepath.Join(data, "models") {
		t.Fatal(l.ReceiveDir())
	}
}

func TestTheExpertOffloadIsPassedOnRequest(t *testing.T) {
	twelve := 12
	args := loadArgs(t, iris, 5<<30, func(o *LlamaServerOptions) {
		o.ModelBackends = map[string]ModelBackend{"m": {NCPUMoe: &twelve}}
	})
	if len(args) != 1 {
		t.Fatalf("an explicit placement has no fallback plans: %v", args)
	}
	a := strings.Join(args[0], " ")
	for _, want := range []string{"--n-cpu-moe 12", "-ngl all", "--fit off"} {
		if !strings.Contains(a, want) {
			t.Errorf("missing %q: %v", want, args[0])
		}
	}
	plain := loadArgs(t, iris, 5<<30, nil)
	if slices.Contains(plain[0], "--n-cpu-moe") {
		t.Errorf("expert offload is an opt-in: %v", plain[0])
	}
}

func TestEmbeddingModelsStartAsEmbeddingServers(t *testing.T) {
	l, _ := newTestServer(t, "cpu", &fakeStarter{})
	plan := loadPlan{cpu: true}
	chat := strings.Join(l.serverArgs("m.gguf", "m", 1, 4096, 2, true, false, plan, "key"), " ")
	if !strings.Contains(chat, "--jinja") || strings.Contains(chat, "--embeddings") {
		t.Fatalf("chat model args %q", chat)
	}
	emb := strings.Join(l.serverArgs("e.gguf", "e", 1, 4096, 2, true, true, plan, "key"), " ")
	for _, want := range []string{"--embeddings", "-b 4096", "-ub 4096", "-c 8192"} {
		if !strings.Contains(emb, want) {
			t.Fatalf("embedding args lack %q: %q", want, emb)
		}
	}
	if strings.Contains(emb, "jinja") {
		t.Fatalf("an embedding model has no chat template: %q", emb)
	}
}

// A load that asks for "at least" a context gets that context: the node grows it only when the
// config names a cap (max_context_length), so a small need is served at its smallest size.
func TestAWorkThatAsksForAtLeastGetsItsFloorUnlessTheNodeHasACap(t *testing.T) {
	perSlot := func(t *testing.T, mutate func(*LlamaServerOptions)) int {
		fs := &fakeStarter{}
		l, data := newTestServer(t, "cpu", fs, func(o *LlamaServerOptions) {
			o.SysInfo = fixedSys{total: 65536, used: 1024}
			if mutate != nil {
				mutate(o)
			}
		})
		ggufModel(t, filepath.Join(data, "models"), "m.gguf")
		if _, err := l.Load(context.Background(), "m", LoadOptions{MinContext: 16384}); err != nil {
			t.Fatal(err)
		}
		args := fs.started[0].args
		c, _ := argOf(args, "-c")
		slots := 1
		if np, ok := argOf(args, "-np"); ok {
			slots, _ = strconv.Atoi(np)
		}
		total, _ := strconv.Atoi(c)
		return total / slots
	}
	if got := perSlot(t, nil); got != 16384 {
		t.Fatalf("no cap: the floor the work asked for, got %d", got)
	}
	if got := perSlot(t, func(o *LlamaServerOptions) { o.MaxContext = 32768 }); got <= 16384 {
		t.Fatalf("with a cap and free memory the node grows the context, got %d", got)
	}
}
