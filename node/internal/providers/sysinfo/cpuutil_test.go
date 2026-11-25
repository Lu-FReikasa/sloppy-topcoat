package sysinfo

import (
	"math"
	"testing"
)

func TestCPUMeterNeedsTwoReadings(t *testing.T) {
	var m cpuMeter
	if _, ok := m.percent(1000, 200); ok {
		t.Fatal("the first reading has no delta")
	}
	p, ok := m.percent(2000, 700) // 1000 more time, 500 of it busy
	if !ok || math.Abs(p-50) > 1e-9 {
		t.Fatalf("got %v %v, want 50 true", p, ok)
	}
	p, ok = m.percent(3000, 700) // all idle
	if !ok || p != 0 {
		t.Fatalf("got %v %v, want 0 true", p, ok)
	}
}

func TestCPUMeterIgnoresNoTimeAndCountersThatWentBackwards(t *testing.T) {
	var m cpuMeter
	m.percent(1000, 200)
	if _, ok := m.percent(1000, 200); ok {
		t.Fatal("no time passed: nothing to say")
	}
	if _, ok := m.percent(500, 100); ok {
		t.Fatal("a counter that went backwards starts over")
	}
	if p, ok := m.percent(1500, 600); !ok || math.Abs(p-50) > 1e-9 {
		t.Fatalf("after the restart: got %v %v, want 50 true", p, ok)
	}
}

func TestCPUMeterNeverLeavesZeroToHundred(t *testing.T) {
	var m cpuMeter
	m.percent(0, 0)
	if p, ok := m.percent(100, 150); !ok || p != 100 {
		t.Fatalf("got %v %v, want 100 true", p, ok)
	}
}

func TestParseProcStat(t *testing.T) {
	stat := []byte("cpu  100 20 30 700 50 5 5 90 0 0\ncpu0 50 10 15 350 25 2 3 45 0 0\nintr 1\n")
	total, busy, ok := parseProcStat(stat)
	if !ok {
		t.Fatal("not parsed")
	}
	// total = 100+20+30+700+50+5+5+90 = 1000; idle = 700+50
	if total != 1000 || busy != 250 {
		t.Fatalf("total %v busy %v, want 1000 and 250", total, busy)
	}
}

func TestParseProcStatRefusesWhatIsNotTheCPULine(t *testing.T) {
	for _, in := range []string{"", "cpu0 1 2 3 4 5\n", "cpu  1 2 3\n", "cpu  a b c d e\n"} {
		if _, _, ok := parseProcStat([]byte(in)); ok {
			t.Fatalf("%q parsed", in)
		}
	}
}
