package activity

import "testing"

func ptr(f float64) *float64 { return &f }

func TestForeignCPUPercent(t *testing.T) {
	for _, c := range []struct {
		busy, own, total float64
		want             float64
		ok               bool
	}{
		{50, 20, 100, 30, true}, // 50 % busy, 20 points are ours
		{20, 30, 100, 0, true},  // own > busy (measurement skew): never negative
		{300, 0, 100, 100, true},
		{10, 0, 0, 0, false}, // no elapsed time yet
	} {
		got, ok := ForeignCPUPercent(c.busy, c.own, c.total)
		if got != c.want || ok != c.ok {
			t.Errorf("%+v: got %v %v", c, got, ok)
		}
	}
}

func TestForeignLoadOnAHybridCPUReachesTheThresholdWithProcessorUtility(t *testing.T) {
	// twelve busy loops on 2 P + 8 E cores: 67 % busy time, but Processor Utility says 100
	full := CPULoad{Time: 67.5, Own: 0, Utility: ptr(100)}
	if got := full.Foreign(); got != 100 {
		t.Fatalf("utility: %v", got)
	}
	// without the utility counter only the time is left — which is why the default is below 67
	if got := (CPULoad{Time: 67.5}).Foreign(); got != 67.5 {
		t.Fatalf("time: %v", got)
	}
}

func TestOwnUseIsTakenOutInTheWeightingOfTheMeasure(t *testing.T) {
	// the node's model uses 30 points of time; utility is 1.2× the busy time (turbo)
	l := CPULoad{Time: 50, Own: 30, Utility: ptr(60)}
	if got, want := l.Foreign(), 60-30*1.2; got < want-1e-9 || got > want+1e-9 { // 24, more than 50-30 = 20
		t.Fatalf("got %v want %v", got, want)
	}
	// nothing but the node itself: nothing foreign
	if got := (CPULoad{Time: 30, Own: 30, Utility: ptr(36)}).Foreign(); got > 1e-9 {
		t.Fatalf("own only: %v", got)
	}
}

func TestALoadInsideAVirtualMachineCountsAsForeign(t *testing.T) {
	// WSL2: the host is nearly idle (8 %), the guest burns every processor
	if got := (CPULoad{Time: 8, HyperV: ptr(100)}).Foreign(); got != 100 {
		t.Fatalf("hypervisor counter: %v", got)
	}
	// only the vmmem process time is readable: a floor of what the guest used
	if got := (CPULoad{Time: 8, Guest: 92}).Foreign(); got != 92 {
		t.Fatalf("vmmem: %v", got)
	}
	// the node's model on the CPU next to an idle guest stays its own load
	if got := (CPULoad{Time: 40, Own: 40, HyperV: ptr(41), Guest: 1}).Foreign(); got > 1.0+1e-9 {
		t.Fatalf("own with idle guest: %v", got)
	}
}

func TestForeignLoadIsClamped(t *testing.T) {
	if got := (CPULoad{Time: 90, Own: 0, Utility: ptr(140)}).Foreign(); got != 100 {
		t.Fatalf("%v", got)
	}
	if got := (CPULoad{Time: 10, Own: 30}).Foreign(); got != 0 {
		t.Fatalf("%v", got)
	}
}

func TestGuestHostProcessNames(t *testing.T) {
	for name, want := range map[string]bool{
		"vmmem": true, "VmmemWSL": true, "vmmemCmFuncs": true, "vmmem.exe": true,
		"vmware-vmx.exe": false, "llama-server.exe": false, "": false,
	} {
		if got := isGuestHost(name); got != want {
			t.Errorf("%q: %v", name, got)
		}
	}
}
