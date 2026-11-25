package sysinfo

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// linuxTopology reads the physical core layout from sysfs.
//
// Hybrid Intel CPUs expose /sys/devices/cpu_core/cpus (P-cores) and
// /sys/devices/cpu_atom/cpus (E-cores). Otherwise a per-CPU cpu_capacity (ARM
// big.LITTLE, some newer kernels on x86) ranks the cores; without either all
// cores are performance cores.
//
// UNVERIFIED on real hardware (hybrid layouts); tested with sysfs fixtures.
func linuxTopology(cpuRoot, devicesRoot string) []coreInfo {
	entries, err := os.ReadDir(cpuRoot)
	if err != nil {
		return nil
	}
	pcores := readCPUList(filepath.Join(devicesRoot, "cpu_core", "cpus"))
	ecores := readCPUList(filepath.Join(devicesRoot, "cpu_atom", "cpus"))
	hybrid := len(pcores) > 0 && len(ecores) > 0
	type key struct{ pkg, core string }
	cores := map[key]*coreInfo{}
	var order []key
	for _, e := range entries {
		n, ok := strings.CutPrefix(e.Name(), "cpu")
		id, err := strconv.Atoi(n)
		if !ok || err != nil {
			continue
		}
		dir := filepath.Join(cpuRoot, e.Name())
		if online := readTrim(filepath.Join(dir, "online")); online == "0" {
			continue
		}
		k := key{readTrim(filepath.Join(dir, "topology", "physical_package_id")), readTrim(filepath.Join(dir, "topology", "core_id"))}
		if k.core == "" {
			k.core = n // no topology: every logical CPU is its own core
		}
		class := 0
		switch {
		case hybrid && pcores[id]:
			class = 1
		case hybrid:
			class = 0
		default:
			if c, err := strconv.Atoi(readTrim(filepath.Join(dir, "cpu_capacity"))); err == nil {
				class = c
			}
		}
		c, ok := cores[k]
		if !ok {
			c = &coreInfo{class: class}
			cores[k] = c
			order = append(order, k)
		}
		c.logical++
		c.class = max(c.class, class)
	}
	out := make([]coreInfo, 0, len(order))
	for _, k := range order {
		out = append(out, *cores[k])
	}
	return out
}

func readTrim(p string) string {
	b, err := os.ReadFile(p)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// readCPUList parses a kernel CPU list like "0-3,8,10-11".
func readCPUList(p string) map[int]bool {
	return parseCPUList(readTrim(p))
}
