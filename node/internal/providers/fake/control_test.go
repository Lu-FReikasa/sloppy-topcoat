package fake

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	"topcoat/node/internal/providers/gpu"
	"topcoat/node/internal/providers/runner"
)

func runnerOpts() runner.LoadOptions { return runner.LoadOptions{} }

func TestOtherProgramsAndSample(t *testing.T) {
	w, err := NewWorld(DefaultProfile())
	if err != nil {
		t.Fatal(err)
	}
	r := w.Runner()
	if _, err := r.Load(context.Background(), "fake-llm-8b-q4", runnerOpts()); err != nil {
		t.Fatal(err)
	}
	if err := w.SetOther(0, Other{ForeignVRAMMB: 9000, DemotedMB: 1000, ForeignUtil: 80}); err != nil {
		t.Fatal(err)
	}
	gpus, usage, err := gpu.SampleOf(context.Background(), w.GPU(), nil)
	if err != nil {
		t.Fatal(err)
	}
	// 800 desktop + 9000 foreign + 6100 model - 1000 paged out
	if gpus[0].VRAMUsedMB != 14900 {
		t.Fatalf("used %d", gpus[0].VRAMUsedMB)
	}
	u := usage[0]
	if !u.OwnKnown || u.OwnDedicatedMB != 5100 || u.OwnSharedMB != 1000 || !u.ForeignUtilKnown || u.ForeignUtilPercent != 80 {
		t.Fatalf("usage %+v", u)
	}
	// A foreign allocation leaves less room for loads.
	if _, err := r.Load(context.Background(), "fake-llm-14b-q4", runnerOpts()); err == nil {
		t.Fatal("load should fail: not enough free VRAM")
	}
	if err := w.SetOther(1, Other{}); err == nil {
		t.Fatal("gpu 1 does not exist")
	}
	if err := w.SetOther(0, Other{ForeignUtil: 101}); err == nil {
		t.Fatal("invalid utilization accepted")
	}
	// Unmet demand: another program placed in shared memory.
	if err := w.ApplyControl([]byte(`{"gpus":[{"foreign_vram_mb":4844,"foreign_shared_mb":7743}]}`)); err != nil {
		t.Fatal(err)
	}
	_, usage, _ = gpu.SampleOf(context.Background(), w.GPU(), nil)
	if u := usage[0]; !u.ForeignSharedKnown || u.ForeignSharedMaxMB != 7743 {
		t.Fatalf("usage %+v", u)
	}
	if err := w.SetOther(0, Other{ForeignSharedMB: -1}); err == nil {
		t.Fatal("negative shared memory accepted")
	}
}

func TestControlFile(t *testing.T) {
	w, _ := NewWorld(DefaultProfile())
	if err := w.ApplyControl([]byte(`{"gpus":[{"foreign_vram_mb":12000,"foreign_util":90}],"ram_foreign_mb":4000}`)); err != nil {
		t.Fatal(err)
	}
	info, _ := w.SysInfo().Info(context.Background())
	if info.RAMUsedMB != 13000 {
		t.Fatalf("ram used %d", info.RAMUsedMB)
	}
	if err := w.ApplyControl([]byte(`{"gpus":[{"nope":1}]}`)); err == nil {
		t.Fatal("unknown field accepted")
	}

	path := filepath.Join(t.TempDir(), "control.json")
	if err := os.WriteFile(path, []byte(`{"gpus":[{"foreign_vram_mb":1000}]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		w.WatchControl(ctx, path, 10*time.Millisecond, slog.New(slog.NewTextHandler(io.Discard, nil)))
		close(done)
	}()
	deadline := time.Now().Add(2 * time.Second)
	for {
		gpus, _ := w.GPU().GPUs(context.Background())
		if gpus[0].VRAMUsedMB == 1800 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("control file not applied: used %d", gpus[0].VRAMUsedMB)
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	<-done
}
