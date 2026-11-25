package tools

import (
	"os"
	"path/filepath"
	"testing"
)

// A host like node-cpu (2026-11-15): three VMs hold most of the RAM, a container a little.
func TestSystemMemoryNamesWhatHoldsTheRAM(t *testing.T) {
	proc := t.TempDir()
	put := func(name, text string) {
		p := filepath.Join(proc, name)
		_ = os.MkdirAll(filepath.Dir(p), 0o755)
		if err := os.WriteFile(p, []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	put("meminfo", "MemTotal: 32497664 kB\nMemAvailable: 3444736 kB\nAnonPages: 26050756 kB\nCached: 2936815 kB\n"+
		"Shmem: 194727 kB\nSReclaimable: 870424 kB\nSUnreclaim: 422162 kB\nKernelStack: 26816 kB\nPageTables: 92080 kB\n"+
		"SwapTotal: 12582908 kB\nSwapFree: 4922368 kB\n")
	put("spl/kstat/zfs/arcstats", "name type data\nhits 4 100\nsize 4 1073741824\n")
	put("1/comm", "init\n")
	put("1/status", "Name:\tinit\nVmRSS:\t   1024 kB\n")
	put("2/status", "Name:\tkthreadd\n") // a kernel thread: no RSS
	put("100/status", "Name:\tqemu-system-x86\nVmRSS:\t9400320 kB\nVmSwap:\t102400 kB\n")
	put("100/cmdline", "/usr/bin/qemu-system-x86_64\x00-name\x00guest=vm-b,debug-threads=on\x00-m\x008192\x00")
	put("101/status", "Name:\tqemu-system-x86\nVmRSS:\t7575552 kB\n")
	put("101/cmdline", "/usr/bin/qemu-system-x86_64\x00-name\x00vm-c\x00")
	put("200/status", "Name:\tjellyfin\nVmRSS:\t835584 kB\n")
	put("200/cmdline", "/jellyfin/jellyfin\x00--password=secret\x00")
	// as a container node sees a host process from its own cgroup namespace (node-cpu)
	put("200/cgroup", "0::/../0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n")
	put("201/status", "Name:\tjava\nVmRSS:\t409600 kB\n")
	put("201/cgroup", "12:memory:/docker/fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210\n")

	out, err := invoke(t, Options{ProcRoot: proc}, "system.memory", `{"top":3}`)
	if err != nil {
		t.Fatal(err)
	}
	m := out["memory_mb"].(map[string]any)
	if m["total"] != float64(31736) || m["programs"] != float64(25440) || m["swap_used"] != float64(7480) {
		t.Fatalf("memory: %v", m)
	}
	if out["zfs_arc_mb"] != float64(1024) || out["processes_seen"] != float64(5) {
		t.Fatalf("%v", out)
	}
	top := out["top_processes"].([]any)
	first := top[0].(map[string]any)
	if len(top) != 3 || first["vm"] != "vm-b" || first["rss_mb"] != float64(9180) || first["swap_mb"] != float64(100) {
		t.Fatalf("top: %v", top)
	}
	if top[1].(map[string]any)["vm"] != "vm-c" || top[2].(map[string]any)["container"] != "0123456789ab" {
		t.Fatalf("top: %v", top)
	}
	if out["containers_mb"].(map[string]any)["fedcba987654"] != float64(400) {
		t.Fatalf("%v", out["containers_mb"])
	}
	if out["virtual_machines_mb"].(map[string]any)["vm-b"] != float64(9180) {
		t.Fatalf("%v", out["virtual_machines_mb"])
	}
	if _, ok := out["note"]; ok {
		t.Fatalf("a host (PID 1 is init) needs no note: %v", out["note"])
	}
	// a command line never leaves the node
	for _, p := range top {
		for k := range p.(map[string]any) {
			if k == "cmdline" || k == "args" {
				t.Fatalf("command line returned: %v", p)
			}
		}
	}
}

func TestSystemMemoryInAContainerSaysWhatItCannotSee(t *testing.T) {
	proc := t.TempDir()
	_ = os.MkdirAll(filepath.Join(proc, "1"), 0o755)
	_ = os.WriteFile(filepath.Join(proc, "meminfo"), []byte("MemTotal: 1024 kB\nMemAvailable: 512 kB\n"), 0o600)
	_ = os.WriteFile(filepath.Join(proc, "1", "comm"), []byte("docker-init\n"), 0o600)
	_ = os.WriteFile(filepath.Join(proc, "1", "status"), []byte("Name:\tdocker-init\nVmRSS:\t100 kB\n"), 0o600)
	out, err := invoke(t, Options{ProcRoot: proc}, "system.memory", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	if out["note"] == nil {
		t.Fatalf("no note in a container: %v", out)
	}
	// with the host's /proc mounted the note goes
	out, err = invoke(t, Options{ProcRoot: t.TempDir(), HostProc: proc}, "system.memory", `{}`)
	if err != nil || out["note"] != nil {
		t.Fatalf("%v %v", out, err)
	}
}
