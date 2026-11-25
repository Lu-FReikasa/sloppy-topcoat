// Package activity measures what the person at this machine is doing, so the node can step back
// (D24 "ducken"): whether they used keyboard or mouse recently, how much CPU other programs use,
// and whether the machine runs on battery. Everything hardware-specific sits behind Provider;
// the fake in providers/fake and the pure calculations here are what the tests use.
package activity

import (
	"context"
	"strings"
	"time"
)

// Sample is one measurement. A signal that cannot be measured on this machine is "not known" and
// never counts as activity, so a node that cannot see input (a Windows service without access
// to the session, a server without a desktop) keeps working on the other signals.
type Sample struct {
	// InputIdle is the time since the last keyboard or mouse input (InputKnown).
	InputIdle  time.Duration
	InputKnown bool
	// InputErr says why input cannot be seen (logged once by the agent).
	InputErr string
	// ForeignCPU is the share of the total CPU that other programs used since the previous
	// sample, in percent of all logical processors (CPUKnown; the first sample has no delta).
	ForeignCPU float64
	CPUKnown   bool
	// CPUNote says which measures the CPU figure is built from and which could not be read (once
	// per change in the agent's log), e.g. "time, utility; hyperv: access denied".
	CPUNote string
	// OnBattery is true when the machine runs on battery power (BatteryKnown; false on machines
	// without a battery).
	OnBattery    bool
	BatteryKnown bool
}

// Provider takes samples. ownPIDs are the node's own processes (the agent, llama-server, ...):
// their CPU use is not "foreign".
type Provider interface {
	Sample(ctx context.Context, ownPIDs []int) Sample
	// Input is only the keyboard/mouse signal: it takes no CPU sample, so it can be asked next
	// to Sample without disturbing the CPU deltas (the idle sleep of E2-05 E does).
	Input(ctx context.Context) (idle time.Duration, known bool)
}

// ForeignCPUPercent is what other programs used: (busy − own) / total, in percent. busy and own
// are CPU time (any unit), total the time all processors together could have delivered in the
// same interval. The result is clamped to 0..100; total ≤ 0 gives (0, false).
func ForeignCPUPercent(busy, own, total float64) (float64, bool) {
	if total <= 0 {
		return 0, false
	}
	foreign := busy - own
	if foreign < 0 {
		foreign = 0
	}
	p := foreign / total * 100
	if p > 100 {
		p = 100
	}
	return p, true
}

// CPULoad is what one interval of measuring gave, all in percent of the machine (all logical
// processors together). Foreign combines the measures; several exist because each one is blind
// somewhere:
//   - Time is the share of time the processors were not idle. On a hybrid CPU under full load it
//     stays well below 100 (67–68 % with twelve busy loops on 2 P + 8 E cores, measured).
//   - Utility is Windows' "Processor Utility", what Task Manager shows: busy time weighted with
//     the clock against the base clock, so it reaches (and can pass) 100 there.
//   - HyperV is the run time of the logical processors including virtual machines, which the
//     host's idle time does not see (WSL2 keeps the host "idle" while a guest burns the CPU).
//   - Guest is the CPU time of the VM host processes (vmmem*) on their own, a floor when the
//     counters above are not readable for the service account.
//
// A measure that could not be taken is nil / 0. Own is the node's own use in the same unit as
// Time.
type CPULoad struct {
	Time, Own float64
	Utility   *float64
	HyperV    *float64
	Guest     float64
}

// Foreign is the highest load other programs cause according to any measure, 0..100.
func (l CPULoad) Foreign() float64 {
	best := max(l.Time-l.Own, l.Guest)
	if l.Utility != nil {
		// our own share in the same weighting as the utility figure
		scale := 1.0
		if l.Time > 0 {
			scale = *l.Utility / l.Time
		}
		best = max(best, *l.Utility-l.Own*scale)
	}
	if l.HyperV != nil {
		best = max(best, *l.HyperV-l.Own)
	}
	return min(max(best, 0), 100)
}

// isGuestHost reports whether an executable name is a virtual-machine host process whose CPU time
// is a guest's: vmmem (WSL2, Hyper-V), VmmemWSL, vmmemCmFuncs …
func isGuestHost(exe string) bool {
	return strings.HasPrefix(strings.ToLower(exe), "vmmem")
}
