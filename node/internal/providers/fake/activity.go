package fake

import (
	"context"
	"sync"
	"time"

	"topcoat/node/internal/providers/activity"
	"topcoat/node/internal/providers/netinfo"
	"topcoat/node/internal/providers/power"
)

// fakeActivity is what the person at the fake machine does (D24): tests and the --fake-control
// file set it to make the node duck or resume.
type fakeActivity struct {
	mu sync.Mutex
	s  activity.Sample
	// Pids records the own-process list of the last sample (tests check that the node passes it).
	pids []int
}

// Activity returns the fake activity provider.
func (w *World) Activity() activity.Provider { return fakeActivityProvider{w} }

type fakeActivityProvider struct{ w *World }

func (p fakeActivityProvider) Sample(_ context.Context, pids []int) activity.Sample {
	p.w.act.mu.Lock()
	defer p.w.act.mu.Unlock()
	p.w.act.pids = append([]int(nil), pids...)
	return p.w.act.s
}

// Input implements activity.Provider.
func (p fakeActivityProvider) Input(context.Context) (time.Duration, bool) {
	p.w.act.mu.Lock()
	defer p.w.act.mu.Unlock()
	return p.w.act.s.InputIdle, p.w.act.s.InputKnown
}

// OwnPIDs are the own processes the node named in its last activity sample.
func (w *World) OwnPIDs() []int {
	w.act.mu.Lock()
	defer w.act.mu.Unlock()
	return append([]int(nil), w.act.pids...)
}

// SetInputIdle sets the time since the last keyboard/mouse input; known=false hides the signal.
func (w *World) SetInputIdle(d time.Duration, known bool) {
	w.act.mu.Lock()
	defer w.act.mu.Unlock()
	w.act.s.InputIdle, w.act.s.InputKnown = d, known
}

// SetForeignCPU sets the share of the CPU other programs use, in percent.
func (w *World) SetForeignCPU(percent float64, known bool) {
	w.act.mu.Lock()
	defer w.act.mu.Unlock()
	w.act.s.ForeignCPU, w.act.s.CPUKnown = percent, known
}

// SetCPUNote sets the description of the CPU measures the fake machine says it uses.
func (w *World) SetCPUNote(note string) {
	w.act.mu.Lock()
	defer w.act.mu.Unlock()
	w.act.s.CPUNote = note
}

// SetOnBattery sets whether the fake machine runs on battery (known=false: it has none).
func (w *World) SetOnBattery(on, known bool) {
	w.act.mu.Lock()
	defer w.act.mu.Unlock()
	w.act.s.OnBattery, w.act.s.BatteryKnown = on, known
}

// SetInputErr makes the input signal unavailable for a stated reason.
func (w *World) SetInputErr(why string) {
	w.act.mu.Lock()
	defer w.act.mu.Unlock()
	w.act.s.InputErr = why
}

// Freeze / Thaw implement runner.Freezer: the fake models stop answering (streams stand still)
// until thawed, like a suspended llama-server.
func (f fakeRunner) Freeze(context.Context) error {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	if !f.w.frozen {
		f.w.frozen = true
		f.w.thawed = make(chan struct{})
	}
	return nil
}

func (f fakeRunner) Thaw(context.Context) error {
	f.w.mu.Lock()
	defer f.w.mu.Unlock()
	if f.w.frozen {
		f.w.frozen = false
		close(f.w.thawed)
	}
	return nil
}

// Frozen reports the state (tests).
func (w *World) Frozen() bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.frozen
}

// waitThawed blocks while the models are frozen; false if ctx ended first.
func (w *World) waitThawed(ctx context.Context) bool {
	w.mu.Lock()
	ch := w.thawed
	w.mu.Unlock()
	select {
	case <-ch:
		return true
	case <-ctx.Done():
		return false
	}
}

// Network returns the fake network interfaces (placeholder addresses, never a real one).
func (w *World) Network() netinfo.Provider { return fakeNet{} }

type fakeNet struct{}

func (fakeNet) Interfaces() []netinfo.Iface {
	return []netinfo.Iface{
		{Name: "eth0", MAC: "02:00:00:00:00:01", Kind: netinfo.Ethernet},
		{Name: "wlan0", MAC: "02:00:00:00:00:02", Kind: netinfo.WiFi},
	}
}

// Sleeper returns the fake sleeper: it only counts (Slept).
func (w *World) Sleeper() power.Sleeper { return fakeSleeper{w} }

type fakeSleeper struct{ w *World }

func (s fakeSleeper) Sleep(context.Context) error {
	s.w.mu.Lock()
	defer s.w.mu.Unlock()
	s.w.slept++
	return nil
}

// Slept counts the sleeps the fake machine went through.
func (w *World) Slept() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.slept
}
