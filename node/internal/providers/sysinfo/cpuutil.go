package sysinfo

import (
	"bufio"
	"bytes"
	"strconv"
	"strings"
	"sync"
)

// cpuMeter turns two readings of "CPU time since boot" into a utilisation: the share of the time
// all processors together were not idle between the calls. The first call has no earlier reading
// and gives nothing; a counter that went backwards (a reboot, a wrap) starts over.
type cpuMeter struct {
	mu          sync.Mutex
	have        bool
	total, busy float64
}

// percent is the utilisation since the previous call in percent, 0..100 (known false on the
// first call and when no time passed).
func (m *cpuMeter) percent(total, busy float64) (float64, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	prevTotal, prevBusy, had := m.total, m.busy, m.have
	m.total, m.busy, m.have = total, busy, true
	dTotal, dBusy := total-prevTotal, busy-prevBusy
	if !had || dTotal <= 0 || dBusy < 0 {
		return 0, false
	}
	return min(max(dBusy/dTotal*100, 0), 100), true
}

// parseProcStat reads the first line of /proc/stat ("cpu  user nice system idle iowait irq softirq
// steal …"): the total CPU time of all processors and the part that was not idle (idle and iowait
// count as idle; guest time is already inside user). Kept OS-independent so it is testable
// everywhere.
func parseProcStat(b []byte) (total, busy float64, ok bool) {
	sc := bufio.NewScanner(bytes.NewReader(b))
	if !sc.Scan() {
		return 0, 0, false
	}
	f := strings.Fields(sc.Text())
	if len(f) < 5 || f[0] != "cpu" {
		return 0, 0, false
	}
	var v [8]float64
	for i := 1; i < len(f) && i <= len(v); i++ {
		n, err := strconv.ParseFloat(f[i], 64)
		if err != nil {
			return 0, 0, false
		}
		v[i-1] = n
	}
	for _, n := range v {
		total += n
	}
	idle := v[3] + v[4]
	return total, total - idle, true
}
