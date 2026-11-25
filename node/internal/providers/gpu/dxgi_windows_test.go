package gpu

import (
	"context"
	"os"
	"testing"
)

// Runs the real DXGI + PDH code on the Windows CI runner (which usually only
// has the Microsoft Basic Render Driver, filtered out as a software adapter).
func TestDXGIRealCallsDoNotFail(t *testing.T) {
	d := &DXGI{}
	gpus, err := d.GPUs(context.Background())
	t.Logf("gpus=%+v err=%v", gpus, err)
	for _, g := range gpus {
		if g.Name == "" || g.VRAMTotalMB < 0 || g.VRAMUsedMB < 0 {
			t.Errorf("implausible GPU %+v", g)
		}
	}
}

// Sample must not fail where per-process counters are missing (CI runner).
func TestDXGISampleDoesNotFail(t *testing.T) {
	d := &DXGI{}
	for i := 0; i < 2; i++ { // the second call has utilization rates
		gpus, usage, err := d.Sample(context.Background(), []int{os.Getpid()})
		t.Logf("gpus=%+v usage=%+v err=%v", gpus, usage, err)
		if len(usage) != len(gpus) {
			t.Fatalf("usage not aligned: %d vs %d", len(usage), len(gpus))
		}
	}
}
