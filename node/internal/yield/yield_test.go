package yield

import (
	"testing"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/activity"
)

var t0 = time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)

func cfg() protocol.Yield {
	return protocol.Yield{
		Enabled: true, LowPriority: true,
		Poll: 5 * time.Second, Idle: 60 * time.Second, CPUPercent: 70,
		Sustain: 5 * time.Second, Resume: 30 * time.Second, PauseOnBattery: true,
		UnloadAfterPause: 15 * time.Minute,
	}
}

// at builds a sample sec seconds after t0.
func at(sec int, a activity.Sample) Sample {
	return Sample{Now: t0.Add(time.Duration(sec) * time.Second), Activity: a, Models: 1}
}

func input(idleSec int) activity.Sample {
	return activity.Sample{InputKnown: true, InputIdle: time.Duration(idleSec) * time.Second}
}

func cpu(pct float64) activity.Sample {
	return activity.Sample{InputKnown: true, InputIdle: time.Hour, CPUKnown: true, ForeignCPU: pct}
}

func TestInputFreezesAtOnceAndThawsAfterTheIdleTime(t *testing.T) {
	c := New(cfg())
	if a := c.Step(at(0, input(3600))); a != nil {
		t.Fatalf("nobody there: %+v", a)
	}
	a := c.Step(at(5, input(0))) // a key was pressed
	if a == nil || a.Kind != Freeze || len(a.Reasons) != 1 || a.Reasons[0] != protocol.PauseInput || !c.Paused() {
		t.Fatalf("%+v", a)
	}
	for sec := 10; sec < 60; sec += 5 { // 5 s .. 55 s after the last key: still "here"
		if a := c.Step(at(sec, input(sec-5))); a != nil {
			t.Fatalf("at %d s: %+v", sec, a)
		}
	}
	a = c.Step(at(65, input(60))) // idle for idle_seconds: continue
	if a == nil || a.Kind != Thaw || a.PausedFor != 60*time.Second || c.Paused() {
		t.Fatalf("%+v", a)
	}
}

func TestCPULoadNeedsToSustainAndTheEndNeedsToSettle(t *testing.T) {
	c := New(cfg())
	if a := c.Step(at(0, cpu(95))); a != nil { // a spike alone is nothing
		t.Fatalf("%+v", a)
	}
	if a := c.Step(at(3, cpu(20))); a != nil {
		t.Fatalf("%+v", a)
	}
	c.Step(at(10, cpu(95))) // a new run above starts
	if a := c.Step(at(14, cpu(95))); a != nil {
		t.Fatalf("4 s is less than sustain_seconds: %+v", a)
	}
	a := c.Step(at(15, cpu(95)))
	if a == nil || a.Kind != Freeze || a.Reasons[0] != protocol.PauseCPULoad {
		t.Fatalf("%+v", a)
	}
	// short dips do not thaw; only 30 s below the threshold do
	if a := c.Step(at(20, cpu(10))); a != nil {
		t.Fatalf("%+v", a)
	}
	if a := c.Step(at(30, cpu(90))); a != nil { // the dip ends: the run below restarts
		t.Fatalf("%+v", a)
	}
	c.Step(at(40, cpu(10)))
	if a := c.Step(at(69, cpu(10))); a != nil {
		t.Fatalf("29 s below: %+v", a)
	}
	if a := c.Step(at(70, cpu(10))); a == nil || a.Kind != Thaw {
		t.Fatalf("30 s below: %+v", a)
	}
}

func TestBatteryFreezesAndPlugInThaws(t *testing.T) {
	c := New(cfg())
	batt := activity.Sample{BatteryKnown: true, OnBattery: true}
	if a := c.Step(at(0, batt)); a == nil || a.Kind != Freeze || a.Reasons[0] != protocol.PauseBattery {
		t.Fatalf("%+v", a)
	}
	if a := c.Step(at(5, activity.Sample{BatteryKnown: true})); a == nil || a.Kind != Thaw {
		t.Fatalf("%+v", a)
	}
	off := cfg()
	off.PauseOnBattery = false
	c = New(off)
	if a := c.Step(at(0, batt)); a != nil {
		t.Fatalf("pause_on_battery off: %+v", a)
	}
}

func TestSeveralReasonsAndTheOrderOfThem(t *testing.T) {
	c := New(cfg())
	both := activity.Sample{InputKnown: true, InputIdle: 0, BatteryKnown: true, OnBattery: true}
	a := c.Step(at(0, both))
	if a == nil || len(a.Reasons) != 2 || a.Reasons[0] != protocol.PauseInput || a.Reasons[1] != protocol.PauseBattery {
		t.Fatalf("%+v", a)
	}
	// one goes, the other holds: still paused, no action
	if a := c.Step(at(5, activity.Sample{BatteryKnown: true, OnBattery: true, InputKnown: true, InputIdle: time.Hour})); a != nil || !c.Paused() {
		t.Fatalf("%+v", a)
	}
	if r := c.Reasons(); len(r) != 1 || r[0] != protocol.PauseBattery {
		t.Fatalf("%v", r)
	}
}

func TestUnknownSignalsNeverCountAndOwnWorkHidesTheCPU(t *testing.T) {
	c := New(cfg())
	if a := c.Step(at(0, activity.Sample{})); a != nil {
		t.Fatalf("a node that sees nothing keeps working: %+v", a)
	}
	// the node's own load / benchmark makes the CPU look busy: not a reason
	for sec := 0; sec < 60; sec += 5 {
		s := at(sec, cpu(100))
		s.IgnoreCPU = true
		if a := c.Step(s); a != nil {
			t.Fatalf("own CPU use froze the node at %d s: %+v", sec, a)
		}
	}
}

func TestLongPauseUnloadsOnce(t *testing.T) {
	c := New(cfg())
	c.Step(at(0, input(0)))
	if a := c.Step(at(899, input(0))); a != nil {
		t.Fatalf("%+v", a)
	}
	a := c.Step(at(900, input(0)))
	if a == nil || a.Kind != Unload || a.PausedFor != 15*time.Minute {
		t.Fatalf("%+v", a)
	}
	if a := c.Step(at(905, input(0))); a != nil {
		t.Fatalf("unload must happen once: %+v", a)
	}
	if a := c.Step(at(1000, input(3600))); a == nil || a.Kind != Thaw {
		t.Fatalf("%+v", a)
	}
	// nothing loaded: nothing to unload; unload_after_pause_seconds 0 = never
	c = New(cfg())
	c.Step(at(0, input(0)))
	s := at(2000, input(0))
	s.Models = 0
	if a := c.Step(s); a != nil {
		t.Fatalf("%+v", a)
	}
	never := cfg()
	never.UnloadAfterPause = 0
	c = New(never)
	c.Step(at(0, input(0)))
	if a := c.Step(at(100000, input(0))); a != nil {
		t.Fatalf("%+v", a)
	}
}

func TestDisablingWhilePausedThaws(t *testing.T) {
	c := New(cfg())
	c.Step(at(0, input(0)))
	off := cfg()
	off.Enabled = false
	c.SetConfig(off)
	if a := c.Step(at(5, input(0))); a == nil || a.Kind != Thaw || c.Paused() {
		t.Fatalf("%+v", a)
	}
	if a := c.Step(at(10, input(0))); a != nil {
		t.Fatalf("%+v", a)
	}
}
