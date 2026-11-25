//go:build linux

package netinfo

import "testing"

func TestEthtoolWoLReadsTheDriverAndToleratesWhatItCannotAsk(t *testing.T) {
	if _, known := ethtoolWoL("an-interface-name-that-is-too-long"); known {
		t.Fatal("an impossible name cannot be known")
	}
	if _, known := ethtoolWoL("no-such-if0"); known {
		t.Fatal("an interface that does not exist cannot be known")
	}
	// the loopback has no ethtool driver: the answer is "cannot" or "not known", never a panic
	if supported, _ := ethtoolWoL("lo"); supported {
		t.Fatal("the loopback cannot wake a machine")
	}
	t.Logf("lo: %v", func() [2]bool { s, k := ethtoolWoL("lo"); return [2]bool{s, k} }())
}
