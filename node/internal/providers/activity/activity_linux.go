package activity

import (
	"bufio"
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

// UNVERIFIED on real hardware: the loginctl output format of the maintainer's distribution and
// the desktop's idle hint; the parsing and the arithmetic are tested with recorded samples.

type system struct {
	proc   string // /proc
	power  string // /sys/class/power_supply
	loginc func(ctx context.Context, args ...string) ([]byte, error)
	mono   func() time.Duration

	mu        sync.Mutex
	prevTotal float64
	prevBusy  float64
	prevTicks map[int]float64 // CPU ticks per own process at the previous sample
	havePrev  bool
}

// NewSystem returns the Linux provider: input from systemd-logind's idle hint, CPU from /proc,
// battery from /sys/class/power_supply.
func NewSystem() Provider {
	return &system{
		proc:  "/proc",
		power: "/sys/class/power_supply",
		loginc: func(ctx context.Context, args ...string) ([]byte, error) {
			return exec.CommandContext(ctx, "loginctl", args...).Output() //nolint:gosec // fixed tool, fixed arguments
		},
		mono: monotonicNow,
	}
}

func (s *system) Input(ctx context.Context) (time.Duration, bool) {
	idle, known, _ := s.input(ctx)
	return idle, known
}

func (s *system) Sample(ctx context.Context, ownPIDs []int) Sample {
	var out Sample
	out.InputIdle, out.InputKnown, out.InputErr = s.input(ctx)
	out.ForeignCPU, out.CPUKnown = s.cpu(ownPIDs)
	out.OnBattery, out.BatteryKnown = batteryState(s.power)
	return out
}

// --- CPU ---------------------------------------------------------------------------

// cpuTimes reads the first line of /proc/stat: total and busy jiffies of all processors.
func parseProcStat(b []byte) (total, busy float64, ok bool) {
	sc := bufio.NewScanner(bytes.NewReader(b))
	if !sc.Scan() {
		return 0, 0, false
	}
	f := strings.Fields(sc.Text())
	if len(f) < 5 || f[0] != "cpu" {
		return 0, 0, false
	}
	var v [10]float64
	for i := 1; i < len(f) && i <= 10; i++ {
		n, err := strconv.ParseFloat(f[i], 64)
		if err != nil {
			return 0, 0, false
		}
		v[i-1] = n
	}
	// user nice system idle iowait irq softirq steal (guest is already inside user)
	idle := v[3] + v[4]
	for _, x := range v[:8] {
		total += x
	}
	return total, total - idle, true
}

// parseProcessTicks reads utime+stime (fields 14 and 15) of /proc/<pid>/stat. The command name
// in parentheses may contain spaces, so parsing starts after the last ')'.
func parseProcessTicks(b []byte) (float64, bool) {
	i := bytes.LastIndexByte(b, ')')
	if i < 0 || i+2 > len(b) {
		return 0, false
	}
	f := strings.Fields(string(b[i+2:]))
	if len(f) < 13 { // state is field 3; utime/stime are fields 14/15 = indexes 11/12 here
		return 0, false
	}
	u, err1 := strconv.ParseFloat(f[11], 64)
	st, err2 := strconv.ParseFloat(f[12], 64)
	if err1 != nil || err2 != nil {
		return 0, false
	}
	return u + st, true
}

func (s *system) cpu(ownPIDs []int) (float64, bool) {
	b, err := os.ReadFile(filepath.Join(s.proc, "stat"))
	if err != nil {
		return 0, false
	}
	total, busy, ok := parseProcStat(b)
	if !ok {
		return 0, false
	}
	ticks := make(map[int]float64, len(ownPIDs))
	for _, pid := range ownPIDs {
		pb, err := os.ReadFile(filepath.Join(s.proc, strconv.Itoa(pid), "stat"))
		if err != nil {
			continue // ended meanwhile
		}
		if t, ok := parseProcessTicks(pb); ok {
			ticks[pid] = t
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	prevTotal, prevBusy, prevTicks, had := s.prevTotal, s.prevBusy, s.prevTicks, s.havePrev
	s.prevTotal, s.prevBusy, s.prevTicks, s.havePrev = total, busy, ticks, true
	if !had {
		return 0, false
	}
	// What our processes used since the previous sample, process by process: one that started
	// meanwhile counts from zero, one that ended (or is no longer ours) drops out.
	var own float64
	for pid, t := range ticks {
		own += max(t-prevTicks[pid], 0)
	}
	return ForeignCPUPercent(busy-prevBusy, own, total-prevTotal)
}

// --- battery -----------------------------------------------------------------------

// batteryState looks at /sys/class/power_supply: on battery when a battery exists, no mains
// supply is online and the battery discharges. Unknown without any supply entry.
func batteryState(root string) (onBattery, known bool) {
	entries, err := os.ReadDir(root)
	if err != nil {
		return false, false
	}
	var hasBattery, mainsOnline, discharging bool
	for _, e := range entries {
		read := func(name string) string {
			b, _ := os.ReadFile(filepath.Join(root, e.Name(), name))
			return strings.TrimSpace(string(b))
		}
		switch strings.ToLower(read("type")) {
		case "battery":
			if strings.EqualFold(read("scope"), "device") { // a mouse or headset, not the system
				continue
			}
			hasBattery = true
			if strings.EqualFold(read("status"), "discharging") {
				discharging = true
			}
		case "mains", "usb", "usb_c", "usb_pd", "usb_dcp", "usb_cdp", "usb_aca":
			if read("online") == "1" {
				mainsOnline = true
			}
		}
	}
	if !hasBattery {
		return false, false // a desktop: never "on battery"
	}
	return discharging && !mainsOnline, true
}

// --- input -------------------------------------------------------------------------

// logindSession is what `loginctl show-session` says about one session.
type logindSession struct {
	active, remote, idleHint bool
	idleSince                time.Duration // IdleSinceHintMonotonic; 0 = unknown
}

func parseSession(b []byte) logindSession {
	var s logindSession
	sc := bufio.NewScanner(bytes.NewReader(b))
	for sc.Scan() {
		k, v, ok := strings.Cut(sc.Text(), "=")
		if !ok {
			continue
		}
		switch k {
		case "Active":
			s.active = v == "yes"
		case "Remote":
			s.remote = v == "yes"
		case "IdleHint":
			s.idleHint = v == "yes"
		case "IdleSinceHintMonotonic":
			if us, err := strconv.ParseInt(v, 10, 64); err == nil && us > 0 {
				s.idleSince = time.Duration(us) * time.Microsecond
			}
		}
	}
	return s
}

// idleFromSessions combines the local active sessions: any that is not idle means the user is
// there now; when all are idle the shortest idle time counts. known=false without such a session
// (a headless server), where nobody can be "at the keyboard".
func idleFromSessions(sessions []logindSession, now time.Duration) (idle time.Duration, known bool) {
	first := true
	for _, s := range sessions {
		if !s.active || s.remote {
			continue
		}
		var d time.Duration
		switch {
		case !s.idleHint:
			d = 0
		case s.idleSince > 0 && now >= s.idleSince:
			d = now - s.idleSince
		default:
			d = 24 * time.Hour // idle since a time the desktop did not tell
		}
		if first || d < idle {
			idle, first = d, false
		}
		known = true
	}
	return idle, known
}

func (s *system) input(ctx context.Context) (time.Duration, bool, string) {
	list, err := s.loginc(ctx, "list-sessions", "--no-legend")
	if err != nil {
		return 0, false, "cannot ask systemd-logind about the session (loginctl): " + err.Error()
	}
	var sessions []logindSession
	for _, line := range strings.Split(string(list), "\n") {
		f := strings.Fields(line)
		if len(f) == 0 {
			continue
		}
		b, err := s.loginc(ctx, "show-session", f[0], "-p", "Active", "-p", "Remote", "-p", "IdleHint", "-p", "IdleSinceHintMonotonic")
		if err != nil {
			continue
		}
		sessions = append(sessions, parseSession(b))
	}
	idle, known := idleFromSessions(sessions, s.mono())
	if !known {
		return 0, false, ""
	}
	return idle, true, ""
}

// monotonicNow is CLOCK_MONOTONIC, the clock logind's IdleSinceHintMonotonic is on.
func monotonicNow() time.Duration {
	var ts syscall.Timespec
	if _, _, e := syscall.Syscall(syscall.SYS_CLOCK_GETTIME, 1, uintptr(unsafe.Pointer(&ts)), 0); e != 0 { //nolint:gosec // syscall with a local struct
		return 0
	}
	return time.Duration(ts.Sec)*time.Second + time.Duration(ts.Nsec)
}
