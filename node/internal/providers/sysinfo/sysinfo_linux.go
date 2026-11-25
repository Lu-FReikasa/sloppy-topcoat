package sysinfo

import (
	"context"
	"fmt"
	"os"
	"runtime"
)

// System reads /proc and /sys on Linux.
type System struct {
	CPUInfoPath string
	MemInfoPath string
	CPURoot     string // /sys/devices/system/cpu
	DevicesRoot string // /sys/devices
	StatPath    string // /proc/stat: the CPU time of all processors
	cpu         cpuMeter
}

// NewSystem returns the system info provider for Linux.
func NewSystem() *System {
	return &System{
		CPUInfoPath: "/proc/cpuinfo", MemInfoPath: "/proc/meminfo",
		CPURoot: "/sys/devices/system/cpu", DevicesRoot: "/sys/devices", StatPath: "/proc/stat",
	}
}

// Info implements Provider.
func (s *System) Info(context.Context) (Info, error) {
	info := Info{CPUModel: "unknown", Cores: runtime.NumCPU(), Threads: runtime.NumCPU()}
	if b, err := os.ReadFile(s.CPUInfoPath); err == nil {
		model, logical := parseCPUInfo(b)
		if model != "" {
			info.CPUModel = model
		}
		if logical > 0 {
			info.Cores, info.Threads = logical, logical
		}
	}
	info.PerformanceCores = info.Cores
	if cores := linuxTopology(s.CPURoot, s.DevicesRoot); len(cores) > 0 {
		info.Cores, info.Threads, info.PerformanceCores, info.EfficiencyCores = summarize(cores)
	}
	if b, err := os.ReadFile(s.StatPath); err == nil {
		if total, busy, ok := parseProcStat(b); ok {
			info.CPUPercent, info.CPUKnown = s.cpu.percent(total, busy)
		}
	}
	b, err := os.ReadFile(s.MemInfoPath)
	if err != nil {
		return info, fmt.Errorf("read meminfo: %w", err)
	}
	info.RAMTotalMB, info.RAMUsedMB, err = parseMemInfo(b)
	return info, err
}
