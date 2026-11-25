package activity

import (
	"fmt"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
)

// UNVERIFIED on real hardware: which of these counters the service account NT SERVICE\topcoat-node
// may read (Performance Monitor Users covers the processor counters; the Hyper-V counters may need
// more), and whether vmmem can be opened from it. What cannot be read is left out of the CPU load
// and named once in the log (Sample.CPUNote); the time-based measure always works.

var (
	pdhDLL                 = windows.NewLazySystemDLL("pdh.dll")
	procPdhOpenQuery       = pdhDLL.NewProc("PdhOpenQueryW")
	procPdhAddEnglishCntr  = pdhDLL.NewProc("PdhAddEnglishCounterW")
	procPdhCollectQuery    = pdhDLL.NewProc("PdhCollectQueryData")
	procPdhGetFormattedVal = pdhDLL.NewProc("PdhGetFormattedCounterValue")
)

const (
	pdhFmtDouble = 0x00000200
	// the English paths, so a localized Windows finds them (PdhAddEnglishCounterW)
	utilityCounter = `\Processor Information(_Total)\% Processor Utility`
	hyperVCounter  = `\Hyper-V Hypervisor Logical Processor(_Total)\% Total Run Time`
)

// pdhValue is PDH_FMT_COUNTERVALUE with a double.
type pdhValue struct {
	CStatus uint32
	_       uint32
	Double  float64
}

// pdhCPU holds one PDH query with the two CPU counters that the plain busy time does not give.
type pdhCPU struct {
	q       uintptr
	utility uintptr // 0 = not available
	hyperV  uintptr
	notes   []string // why a counter is missing
}

func openPDHCPU() *pdhCPU {
	p := &pdhCPU{}
	if err := procPdhOpenQuery.Find(); err != nil {
		p.notes = append(p.notes, "counters: "+err.Error())
		return p
	}
	if r, _, _ := procPdhOpenQuery.Call(0, 0, uintptr(unsafe.Pointer(&p.q))); r != 0 {
		p.q = 0
		p.notes = append(p.notes, fmt.Sprintf("counters: PdhOpenQuery 0x%08x", uint32(r)))
		return p
	}
	add := func(path, name string) uintptr {
		pp, err := windows.UTF16PtrFromString(path)
		if err != nil {
			return 0
		}
		var c uintptr
		if r, _, _ := procPdhAddEnglishCntr.Call(p.q, uintptr(unsafe.Pointer(pp)), 0, uintptr(unsafe.Pointer(&c))); r != 0 {
			p.notes = append(p.notes, fmt.Sprintf("%s: 0x%08x", name, uint32(r)))
			return 0
		}
		return c
	}
	p.utility = add(utilityCounter, "utility")
	p.hyperV = add(hyperVCounter, "hyperv")
	return p
}

// read collects once and returns the two figures; nil where a counter is missing or has no value
// yet (a rate counter needs two collections, so the first read of a query has nothing).
func (p *pdhCPU) read() (utility, hyperV *float64) {
	if p == nil || p.q == 0 {
		return nil, nil
	}
	if r, _, _ := procPdhCollectQuery.Call(p.q); r != 0 {
		return nil, nil
	}
	get := func(c uintptr) *float64 {
		if c == 0 {
			return nil
		}
		var v pdhValue
		if r, _, _ := procPdhGetFormattedVal.Call(c, pdhFmtDouble, 0, uintptr(unsafe.Pointer(&v))); r != 0 || (v.CStatus != 0 && v.CStatus != 1) {
			return nil
		}
		return &v.Double
	}
	return get(p.utility), get(p.hyperV)
}

// note describes the counters this machine offers.
func (p *pdhCPU) note() string {
	parts := []string{"time"}
	if p.utility != 0 {
		parts = append(parts, "utility")
	}
	if p.hyperV != 0 {
		parts = append(parts, "hyperv")
	}
	s := strings.Join(parts, ", ")
	if len(p.notes) > 0 {
		s += "; not available: " + strings.Join(p.notes, ", ")
	}
	return s
}

// guestTimes returns the CPU time (100 ns) of every VM host process (vmmem, VmmemWSL, …) by pid,
// and how many of them could not be opened. Their time is what a guest such as WSL2 used; the
// host's idle time does not show it.
func guestTimes() (times map[int]float64, unreadable int) {
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return nil, 0
	}
	defer windows.CloseHandle(snap) //nolint:errcheck // best effort
	times = map[int]float64{}
	var e windows.ProcessEntry32
	e.Size = uint32(unsafe.Sizeof(e))
	for err := windows.Process32First(snap, &e); err == nil; err = windows.Process32Next(snap, &e) {
		if !isGuestHost(windows.UTF16ToString(e.ExeFile[:])) {
			continue
		}
		if t, ok := processTimes(int(e.ProcessID)); ok {
			times[int(e.ProcessID)] = t
		} else {
			unreadable++
		}
	}
	return times, unreadable
}
