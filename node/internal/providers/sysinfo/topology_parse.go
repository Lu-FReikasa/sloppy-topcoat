package sysinfo

import (
	"encoding/binary"
	"math/bits"
	"strconv"
	"strings"
)

// parseProcessorCores parses the buffer returned by
// GetLogicalProcessorInformationEx(RelationProcessorCore) on 64-bit Windows:
// a sequence of SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX records
//
//	Relationship uint32, Size uint32, then PROCESSOR_RELATIONSHIP:
//	Flags byte, EfficiencyClass byte, Reserved [20]byte, GroupCount uint16,
//	GroupMask [GroupCount]GROUP_AFFINITY{Mask uint64, Group uint16, Reserved [3]uint16}
//
// EfficiencyClass is higher for faster cores (P-cores on Intel hybrid CPUs);
// on non-hybrid CPUs all cores report the same class. Kept OS-independent so
// it is testable everywhere.
func parseProcessorCores(buf []byte) []coreInfo {
	var out []coreInfo
	le := binary.LittleEndian
	for off := 0; off+8 <= len(buf); {
		rel := le.Uint32(buf[off:])
		size := int(le.Uint32(buf[off+4:]))
		if size < 32 || off+size > len(buf) {
			break
		}
		rec := buf[off : off+size]
		if rel == 0 { // RelationProcessorCore
			class := int(rec[9])
			groups := int(le.Uint16(rec[30:]))
			logical := 0
			for g := 0; g < groups; g++ {
				m := 32 + g*16
				if m+8 > len(rec) {
					break
				}
				logical += bits.OnesCount64(le.Uint64(rec[m:]))
			}
			out = append(out, coreInfo{logical: max(logical, 1), class: class})
		}
		off += size
	}
	return out
}

// parseCPUList parses a kernel CPU list like "0-3,8,10-11".
func parseCPUList(s string) map[int]bool {
	out := map[int]bool{}
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		lo, hi, isRange := strings.Cut(part, "-")
		a, err1 := strconv.Atoi(lo)
		b := a
		var err2 error
		if isRange {
			b, err2 = strconv.Atoi(hi)
		}
		if err1 != nil || err2 != nil || b < a {
			continue
		}
		for i := a; i <= b; i++ {
			out[i] = true
		}
	}
	return out
}
