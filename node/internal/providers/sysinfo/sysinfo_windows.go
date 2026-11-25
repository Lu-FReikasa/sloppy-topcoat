package sysinfo

import (
	"context"
	"runtime"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

// System reports system info on Windows: GlobalMemoryStatusEx for RAM, the
// registry for the CPU name and GetLogicalProcessorInformationEx for the
// P/E-core layout. No CGO.
//
// UNVERIFIED on real hardware.
type System struct{ cpu cpuMeter }

// NewSystem returns the system info provider for Windows.
func NewSystem() *System { return &System{} }

// Info implements Provider.
func (s *System) Info(context.Context) (Info, error) {
	info := Info{CPUModel: "unknown", Cores: runtime.NumCPU(), Threads: runtime.NumCPU(), PerformanceCores: runtime.NumCPU()}
	if k, err := registry.OpenKey(registry.LOCAL_MACHINE, `HARDWARE\DESCRIPTION\System\CentralProcessor\0`, registry.QUERY_VALUE); err == nil {
		if v, _, err := k.GetStringValue("ProcessorNameString"); err == nil && strings.TrimSpace(v) != "" {
			info.CPUModel = strings.TrimSpace(v)
		}
		_ = k.Close()
	}
	if cores, err := windowsTopology(); err == nil && len(cores) > 0 {
		info.Cores, info.Threads, info.PerformanceCores, info.EfficiencyCores = summarize(cores)
	}
	if total, busy, ok := systemTimes(); ok {
		info.CPUPercent, info.CPUKnown = s.cpu.percent(total, busy)
	}
	ms := memoryStatusEx{Length: uint32(unsafe.Sizeof(memoryStatusEx{}))}
	if r, _, err := procGlobalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&ms))); r == 0 {
		return info, err
	}
	info.RAMTotalMB = int64(ms.TotalPhys >> 20)
	info.RAMUsedMB = int64((ms.TotalPhys - ms.AvailPhys) >> 20)
	return info, nil
}

// memoryStatusEx is MEMORYSTATUSEX.
type memoryStatusEx struct {
	Length               uint32
	MemoryLoad           uint32
	TotalPhys            uint64
	AvailPhys            uint64
	TotalPageFile        uint64
	AvailPageFile        uint64
	TotalVirtual         uint64
	AvailVirtual         uint64
	AvailExtendedVirtual uint64
}

var procGlobalMemoryStatusEx = windows.NewLazySystemDLL("kernel32.dll").NewProc("GlobalMemoryStatusEx")

var procGetSystemTimes = windows.NewLazySystemDLL("kernel32.dll").NewProc("GetSystemTimes")

// systemTimes is the CPU time of all processors since boot (GetSystemTimes, in 100 ns): the total
// (kernel, which includes idle, plus user) and the busy part (total minus idle).
// UNVERIFIED on real hardware: the arithmetic is tested, the call is not run in the tests.
func systemTimes() (total, busy float64, ok bool) {
	var idle, kernel, user windows.Filetime
	r, _, _ := procGetSystemTimes.Call(
		uintptr(unsafe.Pointer(&idle)), uintptr(unsafe.Pointer(&kernel)), uintptr(unsafe.Pointer(&user)))
	if r == 0 {
		return 0, 0, false
	}
	ticks := func(f windows.Filetime) float64 { return float64(uint64(f.HighDateTime)<<32 | uint64(f.LowDateTime)) }
	total = ticks(kernel) + ticks(user)
	return total, total - ticks(idle), true
}
