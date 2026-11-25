package sysinfo

import (
	"context"
	"runtime"
	"testing"
)

func TestWindowsSystemInfo(t *testing.T) {
	info, err := NewSystem().Info(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("%+v", info)
	if info.RAMTotalMB <= 0 || info.RAMUsedMB <= 0 || info.RAMUsedMB > info.RAMTotalMB {
		t.Fatalf("ram %+v", info)
	}
	if info.Threads != runtime.NumCPU() || info.Cores < 1 || info.PerformanceCores+info.EfficiencyCores != info.Cores {
		t.Fatalf("topology %+v (NumCPU %d)", info, runtime.NumCPU())
	}
	if info.CPUModel == "unknown" {
		t.Fatal("cpu model not read from the registry")
	}
}
