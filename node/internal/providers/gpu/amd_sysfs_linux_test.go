package gpu

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func card(t *testing.T, root, name string, files map[string]string) {
	t.Helper()
	dev := filepath.Join(root, name, "device")
	if err := os.MkdirAll(dev, 0o755); err != nil {
		t.Fatal(err)
	}
	for k, v := range files {
		_ = os.WriteFile(filepath.Join(dev, k), []byte(v+"\n"), 0o644)
	}
}

func TestAMDSysfs(t *testing.T) {
	root := t.TempDir()
	card(t, root, "card0", map[string]string{"vendor": "0x10de"}) // NVIDIA: handled by NVML
	card(t, root, "card1", map[string]string{
		"vendor": "0x1002", "device": "0x7550", "mem_info_vram_total": "17095983104", "mem_info_vram_used": "1073741824",
	})
	card(t, root, "card2", map[string]string{
		"vendor": "0x1002", "product_name": "AMD Radeon Graphics", "mem_info_vram_total": "536870912", "mem_info_vram_used": "268435456",
		"mem_info_gtt_total": "16777216000", "mem_info_gtt_used": "2147483648",
	})
	_ = os.MkdirAll(filepath.Join(root, "card1-DP-1"), 0o755)
	gpus, err := AMDSysfs{Root: root}.GPUs(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(gpus) != 2 {
		t.Fatalf("%+v", gpus)
	}
	if g := gpus[0]; g.Name != "AMD GPU 0x7550" || g.VRAMTotalMB != 16304 || g.VRAMUsedMB != 1024 || g.Integrated {
		t.Fatalf("%+v", g)
	}
	if g := gpus[1]; !g.Integrated || g.SharedTotalMB != 16000 || g.SharedUsedMB != 2048 || g.Name != "AMD Radeon Graphics" {
		t.Fatalf("%+v", g)
	}
}

func TestNVMLAbsentIsNoGPU(t *testing.T) {
	// In CI (no NVIDIA driver) libnvidia-ml.so.1 is missing: no GPUs, no error.
	if _, err := os.Stat("/dev/nvidiactl"); err == nil {
		t.Skip("machine has an NVIDIA driver")
	}
	gpus, err := NVML{}.GPUs(context.Background())
	if err != nil || len(gpus) != 0 {
		t.Fatalf("%+v %v", gpus, err)
	}
}

func TestAMDSampleFromFDInfo(t *testing.T) {
	root, proc := t.TempDir(), t.TempDir()
	// card1/device -> ../pci/0000:03:00.0 like in sysfs.
	pci := filepath.Join(root, "pci", "0000:03:00.0")
	if err := os.MkdirAll(pci, 0o755); err != nil {
		t.Fatal(err)
	}
	for k, v := range map[string]string{"vendor": "0x1002", "mem_info_vram_total": "17095983104",
		"mem_info_vram_used": "12884901888", "gpu_busy_percent": "64"} {
		_ = os.WriteFile(filepath.Join(pci, k), []byte(v+"\n"), 0o644)
	}
	if err := os.MkdirAll(filepath.Join(root, "card1"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(pci, filepath.Join(root, "card1", "device")); err != nil {
		t.Fatal(err)
	}
	fd := func(pid, fd, text string) {
		dir := filepath.Join(proc, pid, "fdinfo")
		_ = os.MkdirAll(dir, 0o755)
		_ = os.WriteFile(filepath.Join(dir, fd), []byte(text), 0o644)
	}
	client := "drm-driver:\tamdgpu\ndrm-pdev:\t0000:03:00.0\ndrm-client-id:\t7\ndrm-memory-vram:\t5242880 KiB\ndrm-memory-gtt:\t1048576 KiB\n"
	fd("4242", "5", client)
	fd("4242", "6", client) // same DRM client via a second fd: counted once
	fd("4242", "7", "pos:\t0\nflags:\t02\n")
	fd("5151", "3", "drm-pdev:\t0000:03:00.0\ndrm-client-id:\t9\ndrm-resident-vram:\t2 GiB\ndrm-memory-vram:\t1 KiB\n")

	a := AMDSysfs{Root: root, ProcRoot: proc}
	gpus, usage, err := a.Sample(context.Background(), []int{4242, 5151, 999999})
	if err != nil || len(gpus) != 1 || len(usage) != 1 {
		t.Fatalf("%v %+v %+v", err, gpus, usage)
	}
	u := usage[0]
	if !u.OwnKnown || u.OwnDedicatedMB != 5120+2048 || !u.SharedKnown || u.OwnSharedMB != 1024 {
		t.Fatalf("usage %+v", u)
	}
	if !u.UtilKnown || u.UtilPercent != 64 || u.ForeignUtilKnown {
		t.Fatalf("util %+v", u)
	}
	// Without our processes the whole utilization belongs to other programs.
	_, usage, _ = a.Sample(context.Background(), nil)
	if u := usage[0]; !u.ForeignUtilKnown || u.ForeignUtilPercent != 64 || u.OwnDedicatedMB != 0 {
		t.Fatalf("no pids %+v", u)
	}
	if kib("12 MiB") != 12<<10 || kib("4096") != 4 || kib("x KiB") != 0 || kib("1 TiB") != 0 {
		t.Fatal("kib")
	}
}
