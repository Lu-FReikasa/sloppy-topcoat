package sdcpp

import (
	"testing"

	"topcoat/node/internal/llamacpp"
)

func TestThePinsHaveEveryPlatformTheNodesNeed(t *testing.T) {
	m := Pinned()
	for _, want := range [][2]string{{"windows", "vulkan"}, {"windows", "cuda"}, {"windows", "cpu"}, {"linux", "vulkan"}, {"linux", "cpu"}} {
		if _, err := m.Build(m.Default, want[0], want[1]); err != nil {
			t.Errorf("%v: %v", want, err)
		}
	}
	if _, err := m.Build(m.Default, "linux", "cuda"); err == nil {
		t.Error("there is no official Linux CUDA build; a pin for it would be wrong")
	}
}

func TestLinuxNvidiaUsesVulkan(t *testing.T) {
	if got := SelectBackend("linux", "auto", []string{"nvidia"}); got != llamacpp.BackendVulkan {
		t.Fatalf("linux nvidia: %s", got)
	}
	if got := SelectBackend("windows", "auto", []string{"nvidia"}); got != llamacpp.BackendCUDA {
		t.Fatalf("windows nvidia: %s", got)
	}
	if got := SelectBackend("linux", "auto", nil); got != llamacpp.BackendCPU {
		t.Fatalf("no gpu: %s", got)
	}
}

func TestATagThatIsNoSDTagIsRefused(t *testing.T) {
	if _, err := llamacpp.ParseManifestFor([]byte(`{"default":"b1","base_url":"x","releases":{"b1":{"builds":[]}}}`), tagRe); err == nil {
		t.Fatal("a llama.cpp tag passed as an sd.cpp pin")
	}
}
