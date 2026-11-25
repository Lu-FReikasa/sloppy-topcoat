package gpu

import (
	"strconv"
	"strings"
)

// LUID identifies a display adapter (DXGI_ADAPTER_DESC1.AdapterLuid).
type LUID struct {
	High int32
	Low  uint32
}

// parseLUIDInstance parses a PDH "GPU Adapter Memory" instance name such as
// "luid_0x00000000_0x0000C3F1_phys_0".
func parseLUIDInstance(name string) (LUID, bool) {
	parts := strings.Split(strings.ToLower(name), "_")
	if len(parts) < 3 || parts[0] != "luid" {
		return LUID{}, false
	}
	hi, err1 := strconv.ParseUint(strings.TrimPrefix(parts[1], "0x"), 16, 32)
	lo, err2 := strconv.ParseUint(strings.TrimPrefix(parts[2], "0x"), 16, 32)
	if err1 != nil || err2 != nil {
		return LUID{}, false
	}
	return LUID{High: int32(uint32(hi)), Low: uint32(lo)}, true
}

// sumByLUID adds counter values (bytes) of all instances per adapter.
func sumByLUID(values map[string]int64) map[LUID]int64 {
	out := map[LUID]int64{}
	for name, v := range values {
		if l, ok := parseLUIDInstance(name); ok {
			out[l] += v
		}
	}
	return out
}

// adapter is one DXGI adapter description.
type adapter struct {
	Name        string
	VendorID    uint32
	DedicatedMB int64
	SharedMB    int64
	LUID        LUID
	Software    bool
}

// integratedDedicatedLimitMB: adapters with at most this much dedicated
// memory (Intel Iris Xe reports 128 MB, AMD APUs a small carve-out) are
// integrated GPUs that live on shared system memory.
const integratedDedicatedLimitMB = 2048

func vendorName(id uint32) string {
	switch id {
	case 0x10DE:
		return VendorNVIDIA
	case 0x1002, 0x1022:
		return VendorAMD
	case 0x8086:
		return VendorIntel
	}
	return VendorOther
}

// buildGPUs combines DXGI adapters with PDH usage (bytes per LUID). The LUIDs
// are returned index-aligned with the GPUs.
func buildGPUs(adapters []adapter, dedicated, shared map[LUID]int64) ([]GPU, []LUID) {
	var out []GPU
	var luids []LUID
	for _, a := range adapters {
		if a.Software || a.VendorID == 0x1414 { // Microsoft Basic Render Driver
			continue
		}
		g := GPU{
			Index: len(out), Name: a.Name, Vendor: vendorName(a.VendorID),
			VRAMTotalMB: a.DedicatedMB, VRAMUsedMB: dedicated[a.LUID] >> 20,
		}
		if a.DedicatedMB <= integratedDedicatedLimitMB {
			g.Integrated = true
			g.SharedTotalMB = a.SharedMB
			g.SharedUsedMB = shared[a.LUID] >> 20
		}
		out = append(out, g)
		luids = append(luids, a.LUID)
	}
	return out, luids
}

// processInstance is a parsed per-process PDH instance name:
//
//	GPU Process Memory: pid_7284_luid_0x00000000_0x0000D1A5_phys_0
//	GPU Engine:         pid_7284_luid_0x00000000_0x0000D1A5_phys_0_eng_3_engtype_3D
type processInstance struct {
	PID     int
	LUID    LUID
	Engine  string // "eng_3" (GPU Engine only)
	EngType string // "3D", "Copy", "VideoDecode", ...
}

func parseProcessInstance(name string) (processInstance, bool) {
	parts := strings.Split(strings.ToLower(name), "_")
	if len(parts) < 5 || parts[0] != "pid" || parts[2] != "luid" {
		return processInstance{}, false
	}
	pid, err := strconv.Atoi(parts[1])
	if err != nil || pid < 0 {
		return processInstance{}, false
	}
	luid, ok := parseLUIDInstance(strings.Join(parts[2:5], "_"))
	if !ok {
		return processInstance{}, false
	}
	pi := processInstance{PID: pid, LUID: luid}
	for i := 5; i+1 < len(parts); i++ {
		switch parts[i] {
		case "eng":
			pi.Engine = "eng_" + parts[i+1]
		case "engtype":
			pi.EngType = strings.Join(strings.Split(name, "_")[i+1:], "_")
			i = len(parts)
		}
	}
	return pi, true
}

// ownMemoryByLUID sums per-process memory counters (bytes) of the given pids per adapter.
func ownMemoryByLUID(values map[string]float64, pids map[int]bool) map[LUID]int64 {
	out := map[LUID]int64{}
	for name, v := range values {
		pi, ok := parseProcessInstance(name)
		if ok && pids[pi.PID] {
			out[pi.LUID] += int64(v)
		}
	}
	return out
}

// foreignMaxByLUID returns, per adapter, the largest per-process sum of a
// memory counter (bytes) among processes not in pids.
func foreignMaxByLUID(values map[string]float64, pids map[int]bool) map[LUID]int64 {
	type key struct {
		pid  int
		luid LUID
	}
	sums := map[key]int64{}
	for name, v := range values {
		pi, ok := parseProcessInstance(name)
		if ok && !pids[pi.PID] {
			sums[key{pi.PID, pi.LUID}] += int64(v)
		}
	}
	out := map[LUID]int64{}
	for k, v := range sums {
		out[k.luid] = max(out[k.luid], v)
	}
	return out
}

// engineUtilByLUID computes the utilization per adapter the way Task Manager
// does: per engine the sum over processes, then the busiest engine. It returns
// that total and the same figure without the given pids (other programs).
func engineUtilByLUID(values map[string]float64, pids map[int]bool) (total, foreign map[LUID]float64) {
	type key struct {
		luid   LUID
		engine string
	}
	all, other := map[key]float64{}, map[key]float64{}
	for name, v := range values {
		pi, ok := parseProcessInstance(name)
		if !ok || pi.Engine == "" {
			continue
		}
		k := key{pi.LUID, pi.Engine}
		all[k] += v
		if !pids[pi.PID] {
			other[k] += v
		}
	}
	busiest := func(m map[key]float64) map[LUID]float64 {
		out := map[LUID]float64{}
		for k, v := range m {
			out[k.luid] = max(out[k.luid], min(v, 100))
		}
		return out
	}
	return busiest(all), busiest(other)
}
