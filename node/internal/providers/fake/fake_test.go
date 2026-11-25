package fake

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/providers/runner"
	"topcoat/node/internal/providers/watcher"
)

func TestExampleProfileMatchesDefault(t *testing.T) {
	_, file, _, _ := runtime.Caller(0)
	p, err := LoadProfile(filepath.Join(filepath.Dir(file), "..", "..", "..", "fake-profile.example.json"))
	if err != nil {
		t.Fatal(err)
	}
	d := DefaultProfile()
	if !reflect.DeepEqual(p.GPUs, d.GPUs) || !reflect.DeepEqual(p.Models, d.Models) || p.CPU != d.CPU || p.RAMTotalMB != d.RAMTotalMB {
		t.Fatalf("example profile drifted from DefaultProfile:\n%+v\n%+v", p, d)
	}
}

func TestProfileValidation(t *testing.T) {
	if _, err := ParseProfile([]byte(`{"cpu":{"model":"x","cores":1},"bogus":1}`)); err == nil {
		t.Fatal("unknown field accepted")
	}
	if _, err := ParseProfile([]byte(`{"cpu":{"model":"x","cores":0}}`)); err == nil {
		t.Fatal("zero cores accepted")
	}
	if _, err := ParseProfile([]byte(`{"cpu":{"model":"x","cores":1},"gpus":[{"name":"g","vendor":"acme","vram_total_mb":1}]}`)); err == nil {
		t.Fatal("bad vendor accepted")
	}
	if _, err := ParseProfile([]byte(`{"cpu":{"model":"x","cores":1},"loaded":["m"]}`)); err == nil {
		t.Fatal("unknown preload accepted")
	}
	if _, err := LoadProfile(filepath.Join(t.TempDir(), "missing.json")); err == nil {
		t.Fatal("missing file accepted")
	}
	path := filepath.Join(t.TempDir(), "p.json")
	if err := os.WriteFile(path, []byte(`{"cpu":{"model":"x","cores":2},"ram_total_mb":10,"models":[{"name":"m","vram_mb":1}],"loaded":["m"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	p, err := LoadProfile(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := NewWorld(p); err == nil {
		t.Fatal("preload without GPU should fail")
	}
}

func TestSharedVRAM(t *testing.T) {
	ctx := context.Background()
	w, err := NewWorld(DefaultProfile())
	if err != nil {
		t.Fatal(err)
	}
	r, g := w.Runner(), w.GPU()
	gpus, _ := g.GPUs(ctx)
	if len(gpus) != 1 || gpus[0].VRAMUsedMB != 800 || gpus[0].VRAMTotalMB != 16384 {
		t.Fatalf("initial gpus %+v", gpus)
	}
	lm, err := r.Load(ctx, "fake-llm-8b-q4", runner.LoadOptions{ContextLength: 8192})
	if err != nil || lm.VRAMUsedMB != 6100 || lm.ContextLength != 8192 {
		t.Fatalf("load %+v %v", lm, err)
	}
	gpus, _ = g.GPUs(ctx)
	if gpus[0].VRAMUsedMB != 6900 {
		t.Fatalf("vram used %d", gpus[0].VRAMUsedMB)
	}
	// Idempotent.
	if _, err := r.Load(ctx, "fake-llm-8b-q4", runner.LoadOptions{}); err != nil {
		t.Fatal(err)
	}
	// 800 + 6100 + 9800 > 16384.
	if _, err := r.Load(ctx, "fake-llm-14b-q4", runner.LoadOptions{}); err == nil {
		t.Fatal("expected out of VRAM")
	}
	if _, err := r.Load(ctx, "fake-llm-3b-q8", runner.LoadOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Load(ctx, "nope", runner.LoadOptions{}); !errors.Is(err, runner.ErrUnknownModel) {
		t.Fatalf("unknown model err %v", err)
	}
	loaded, _ := r.Loaded(ctx)
	if len(loaded) != 2 || loaded[0].Name != "fake-llm-8b-q4" {
		t.Fatalf("loaded %+v", loaded)
	}
	if err := r.Unload(ctx, "fake-llm-8b-q4"); err != nil {
		t.Fatal(err)
	}
	if err := r.Unload(ctx, "fake-llm-8b-q4"); !errors.Is(err, runner.ErrNotLoaded) {
		t.Fatalf("double unload %v", err)
	}
	all, err := r.UnloadAll(ctx)
	if err != nil || len(all) != 1 || all[0].Name != "fake-llm-3b-q8" {
		t.Fatalf("unload all %+v %v", all, err)
	}
	gpus, _ = g.GPUs(ctx)
	if gpus[0].VRAMUsedMB != 800 {
		t.Fatalf("vram after unload %d", gpus[0].VRAMUsedMB)
	}
	models, _ := r.ListModels(ctx)
	if len(models) != 3 {
		t.Fatalf("models %+v", models)
	}
	info, _ := w.SysInfo().Info(ctx)
	if info.RAMTotalMB != 32768 || info.Cores != 8 {
		t.Fatalf("sysinfo %+v", info)
	}
	w.SetRunnerAvailable(false)
	if r.Health(ctx) == nil {
		t.Fatal("expected unhealthy")
	}
	if _, err := r.Load(ctx, "fake-llm-3b-q8", runner.LoadOptions{}); err == nil {
		t.Fatal("load on unavailable runner")
	}
	if _, err := r.Download(ctx, runner.DownloadRequest{}); err == nil || r.Delete(ctx, "x") == nil {
		t.Fatal("invalid download / unknown delete accepted")
	}
}

func TestFakeDownloadDeleteAndBench(t *testing.T) {
	p := DefaultProfile()
	p.CPU = ProfileCPU{Model: "i7", Cores: 10, Threads: 12, PerformanceCores: 2, EfficiencyCores: 8}
	p.GPUs = []ProfileGPU{{Name: "Iris Xe", Vendor: "intel", VRAMTotalMB: 16000, VRAMBaseUsedMB: 100, Integrated: true, SharedTotalMB: 16000}}
	w, err := NewWorld(p)
	if err != nil {
		t.Fatal(err)
	}
	w.DownloadStep, w.BenchDelay = 0, 0
	ctx := context.Background()
	r := w.Runner()
	var last int64
	m, err := r.Download(ctx, runner.DownloadRequest{Repo: "org/repo", File: "x/New-Q4.gguf", Progress: func(d, _ int64) { last = d }})
	if err != nil || m.Name != "New-Q4" || last != m.SizeBytes {
		t.Fatalf("%+v %v %d", m, err, last)
	}
	if _, err := r.Download(ctx, runner.DownloadRequest{Repo: "org/repo", File: "New-Q4.gguf"}); !errors.Is(err, runner.ErrModelExists) {
		t.Fatalf("dup: %v", err)
	}
	if _, err := r.Load(ctx, "New-Q4", runner.LoadOptions{}); err != nil {
		t.Fatal(err)
	}
	gpus, _ := w.GPU().GPUs(ctx)
	if !gpus[0].Integrated || gpus[0].SharedUsedMB == 0 || gpus[0].VRAMUsedMB != 100 {
		t.Fatalf("%+v", gpus)
	}
	if err := r.Delete(ctx, "New-Q4"); err != nil {
		t.Fatal(err)
	}
	if loaded, _ := r.Loaded(ctx); len(loaded) != 0 {
		t.Fatal("deleted model still loaded")
	}
	info, _ := w.SysInfo().Info(ctx)
	if info.PerformanceCores != 2 || info.EfficiencyCores != 8 || info.Threads != 12 {
		t.Fatalf("%+v", info)
	}
	if ri := r.(runner.Describer).RuntimeInfo(); ri.Backend != "vulkan" {
		t.Fatalf("%+v", ri)
	}
	bt, err := r.(runner.Benchmarkable).BenchTarget(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := bt.ModelPath("nope"); err == nil {
		t.Fatal("unknown model path")
	}
	out, err := bt.Exec(ctx, bt.Bench, []string{"-t", "2", "-ngl", "0"})
	if err != nil || !strings.Contains(string(out), `"n_threads":2`) {
		t.Fatalf("%s %v", out, err)
	}
}

func TestWatcher(t *testing.T) {
	w := NewWatcher()
	w.Start("Game.EXE") // running before Watch
	ctx, cancel := context.WithCancel(context.Background())
	ch, err := w.Watch(ctx, []watcher.Rule{{Process: "game.exe"}})
	if err != nil {
		t.Fatal(err)
	}
	next := func() watcher.Event {
		select {
		case ev := <-ch:
			return ev
		case <-time.After(2 * time.Second):
			t.Fatal("timeout")
		}
		return watcher.Event{}
	}
	if ev := next(); !ev.Started || ev.Process != "Game.EXE" {
		t.Fatalf("%+v", ev)
	}
	w.Start("other.exe") // not matched
	w.Stop("game.exe")
	if ev := next(); ev.Started {
		t.Fatalf("%+v", ev)
	}
	cancel()
	for range ch {
	}
	if w.Subscribers() != 0 {
		t.Fatal("subscription not removed")
	}
}
