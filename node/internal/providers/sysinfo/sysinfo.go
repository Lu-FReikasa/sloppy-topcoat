// Package sysinfo reports CPU and RAM information.
package sysinfo

import "context"

// Info is a snapshot of CPU / RAM information.
type Info struct {
	CPUModel string
	// Cores is the number of physical cores (logical processors if unknown).
	Cores int
	// Threads is the number of logical processors.
	Threads int
	// PerformanceCores / EfficiencyCores split the physical cores on hybrid
	// CPUs; on other CPUs all cores count as performance cores.
	PerformanceCores int
	EfficiencyCores  int
	RAMTotalMB       int64
	RAMUsedMB        int64
	// CPUPercent is the utilization of all logical processors together since the previous call, in
	// percent (CPUKnown; the first call has no delta and the figure cannot be read on every system).
	CPUPercent float64
	CPUKnown   bool
}

// coreInfo is one physical core: its logical processors and efficiency rank
// (higher = faster core).
type coreInfo struct {
	logical int
	class   int
}

// summarize turns physical cores into counts. The cores with the highest
// class are performance cores; all others efficiency cores.
func summarize(cores []coreInfo) (physical, threads, perf, eff int) {
	maxClass := 0
	for _, c := range cores {
		maxClass = max(maxClass, c.class)
	}
	for _, c := range cores {
		physical++
		threads += c.logical
		if c.class == maxClass {
			perf++
		} else {
			eff++
		}
	}
	return physical, threads, perf, eff
}

// Provider returns system information.
type Provider interface {
	Info(ctx context.Context) (Info, error)
}
