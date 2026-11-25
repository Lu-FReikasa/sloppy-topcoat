package sysinfo

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func TestSystemFromFiles(t *testing.T) {
	dir := t.TempDir()
	cpu := filepath.Join(dir, "cpuinfo")
	mem := filepath.Join(dir, "meminfo")
	if err := os.WriteFile(cpu, []byte(cpuinfo), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(mem, []byte(meminfo), 0o600); err != nil {
		t.Fatal(err)
	}
	s := &System{CPUInfoPath: cpu, MemInfoPath: mem, CPURoot: filepath.Join(dir, "none")}
	info, err := s.Info(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if info.Cores != 2 || info.RAMTotalMB != 32000 || info.CPUModel == "unknown" {
		t.Fatalf("%+v", info)
	}
}

func TestSystemRealProc(t *testing.T) {
	info, err := NewSystem().Info(context.Background())
	if err != nil {
		t.Skipf("no /proc: %v", err)
	}
	if info.Cores < 1 || info.RAMTotalMB <= 0 {
		t.Fatalf("%+v", info)
	}
}

// sysfsCPU writes a fake /sys/devices/system/cpu/cpuN.
func sysfsCPU(t *testing.T, root string, id int, pkg, core string, capacity string) {
	t.Helper()
	d := filepath.Join(root, "cpu", "cpu"+itoa(id), "topology")
	if err := os.MkdirAll(d, 0o755); err != nil {
		t.Fatal(err)
	}
	_ = os.WriteFile(filepath.Join(d, "physical_package_id"), []byte(pkg+"\n"), 0o644)
	_ = os.WriteFile(filepath.Join(d, "core_id"), []byte(core+"\n"), 0o644)
	if capacity != "" {
		_ = os.WriteFile(filepath.Join(root, "cpu", "cpu"+itoa(id), "cpu_capacity"), []byte(capacity), 0o644)
	}
}

func itoa(i int) string { return fmt.Sprint(i) }

func TestLinuxTopologyHybridIntel(t *testing.T) {
	root := t.TempDir()
	// i7-1255U: cpu0-3 = 2 P-cores with HT (core 0, 4), cpu4-11 = 8 E-cores.
	sysfsCPU(t, root, 0, "0", "0", "")
	sysfsCPU(t, root, 1, "0", "0", "")
	sysfsCPU(t, root, 2, "0", "4", "")
	sysfsCPU(t, root, 3, "0", "4", "")
	for i := 4; i < 12; i++ {
		sysfsCPU(t, root, i, "0", itoa(8+i), "")
	}
	_ = os.MkdirAll(filepath.Join(root, "cpu", "cpufreq"), 0o755) // non-cpuN entries are ignored
	devices := filepath.Join(root, "devices")
	_ = os.MkdirAll(filepath.Join(devices, "cpu_core"), 0o755)
	_ = os.MkdirAll(filepath.Join(devices, "cpu_atom"), 0o755)
	_ = os.WriteFile(filepath.Join(devices, "cpu_core", "cpus"), []byte("0-3\n"), 0o644)
	_ = os.WriteFile(filepath.Join(devices, "cpu_atom", "cpus"), []byte("4-11\n"), 0o644)
	phys, threads, p, e := summarize(linuxTopology(filepath.Join(root, "cpu"), devices))
	if got := fmt.Sprint(phys, threads, p, e); got != "10 12 2 8" {
		t.Fatalf("got %s", got)
	}
}

func TestLinuxTopologySMTAndCapacity(t *testing.T) {
	root := t.TempDir()
	// Ryzen 5 3600X: 6 cores, 12 threads (siblings share core_id).
	for i := 0; i < 12; i++ {
		sysfsCPU(t, root, i, "0", itoa(i%6), "")
	}
	phys, threads, p, e := summarize(linuxTopology(filepath.Join(root, "cpu"), filepath.Join(root, "nodevices")))
	if got := fmt.Sprint(phys, threads, p, e); got != "6 12 6 0" {
		t.Fatalf("got %s", got)
	}
	root2 := t.TempDir()
	for i := 0; i < 4; i++ {
		c := "1024"
		if i >= 2 {
			c = "512"
		}
		sysfsCPU(t, root2, i, "0", itoa(i), c)
	}
	phys, threads, p, e = summarize(linuxTopology(filepath.Join(root2, "cpu"), filepath.Join(root2, "x")))
	if got := fmt.Sprint(phys, threads, p, e); got != "4 4 2 2" {
		t.Fatalf("capacity: got %s", got)
	}
}
