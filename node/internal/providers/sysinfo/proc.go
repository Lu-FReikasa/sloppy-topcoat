package sysinfo

import (
	"bufio"
	"bytes"
	"fmt"
	"strconv"
	"strings"
)

// parseCPUInfo extracts the CPU model name and the number of logical CPUs from
// /proc/cpuinfo content. Kept OS-independent so it is testable everywhere.
func parseCPUInfo(b []byte) (model string, cores int) {
	sc := bufio.NewScanner(bytes.NewReader(b))
	for sc.Scan() {
		k, v, ok := strings.Cut(sc.Text(), ":")
		if !ok {
			continue
		}
		k = strings.TrimSpace(k)
		v = strings.TrimSpace(v)
		switch k {
		case "processor":
			cores++
		case "model name", "Model", "Hardware":
			if model == "" {
				model = v
			}
		}
	}
	return model, cores
}

// parseMemInfo returns MemTotal and the memory in use, in MiB, from /proc/meminfo content.
//
// In use is MemTotal - MemAvailable plus the file pages that programs have mapped (Mapped - Shmem). MemAvailable
// counts the whole page cache as available, and a model that llama-server maps from its file sits in that cache
// while the CPU reads it all the time: without the mapped part a node with an 18 GB model loaded reported 17 %
// in use, where Windows (working set of the process) reports the same model as in use. This keeps the figure
// comparable across systems and keeps free = total - used honest for the pressure controller.
func parseMemInfo(b []byte) (totalMB, usedMB int64, err error) {
	vals := map[string]int64{}
	sc := bufio.NewScanner(bytes.NewReader(b))
	for sc.Scan() {
		k, v, ok := strings.Cut(sc.Text(), ":")
		if !ok {
			continue
		}
		fields := strings.Fields(v)
		if len(fields) == 0 {
			continue
		}
		n, perr := strconv.ParseInt(fields[0], 10, 64)
		if perr != nil {
			continue
		}
		vals[strings.TrimSpace(k)] = n // kB
	}
	total, ok := vals["MemTotal"]
	if !ok {
		return 0, 0, fmt.Errorf("meminfo: MemTotal missing")
	}
	avail, ok := vals["MemAvailable"]
	if !ok {
		avail = vals["MemFree"] + vals["Buffers"] + vals["Cached"]
	}
	used := total - avail
	if mapped := vals["Mapped"] - vals["Shmem"]; mapped > 0 { // Shmem is not available either: no double count
		used += mapped
	}
	used = min(max(used, 0), total)
	return total / 1024, used / 1024, nil
}
