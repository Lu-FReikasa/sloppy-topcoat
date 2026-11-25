package gpu

import (
	"context"
	"errors"
	"strings"
	"testing"

	"topcoat/node/internal/providers"
)

type static []GPU

func (s static) GPUs(context.Context) ([]GPU, error) { return s, nil }

type failing struct{}

func (failing) GPUs(context.Context) ([]GPU, error) { return nil, providers.ErrNotImplemented }

func TestMultiReindexesAndKeepsPartialResults(t *testing.T) {
	m := Multi{static{{Index: 7, Name: "a"}}, failing{}, static{{Index: 0, Name: "b"}}}
	gpus, err := m.GPUs(context.Background())
	if len(gpus) != 2 || gpus[0].Index != 0 || gpus[1].Index != 1 || gpus[1].Name != "b" {
		t.Fatalf("%+v", gpus)
	}
	if !errors.Is(err, providers.ErrNotImplemented) {
		t.Fatalf("err %v", err)
	}
}

type partial struct{}

func (partial) GPUs(context.Context) ([]GPU, error) {
	return []GPU{{Name: "RX 9070 XT", VRAMTotalMB: 16304}}, errors.New("PdhAddEnglishCounterW: 0xc0000bdb")
}

// A backend that finds adapters but not their usage counters (Windows service
// account without "Performance Monitor Users") must not lose the adapters.
func TestMultiKeepsGPUsReportedWithAnError(t *testing.T) {
	gpus, err := Multi{partial{}}.GPUs(context.Background())
	if len(gpus) != 1 || gpus[0].Name != "RX 9070 XT" || err == nil {
		t.Fatalf("%+v %v", gpus, err)
	}
	gpus, usage, err := Multi{partial{}}.Sample(context.Background(), nil)
	if len(gpus) != 1 || len(usage) != 1 || err == nil {
		t.Fatalf("%+v %+v %v", gpus, usage, err)
	}
}

func TestParseNvidiaSMI(t *testing.T) {
	gpus, err := parseNvidiaSMI([]byte("0, NVIDIA GeForce RTX 3070, 8192, 1043\n1, Tesla, 16384, 0\n"))
	if err != nil {
		t.Fatal(err)
	}
	if len(gpus) != 2 || gpus[0].Name != "NVIDIA GeForce RTX 3070" || gpus[0].VRAMTotalMB != 8192 || gpus[0].VRAMUsedMB != 1043 || gpus[1].Index != 1 || gpus[0].Vendor != VendorNVIDIA {
		t.Fatalf("%+v", gpus)
	}
	if _, err := parseNvidiaSMI([]byte("0, x, [N/A], 1\n")); err == nil {
		t.Fatal("bad value accepted")
	}
}

func TestNvidiaSMIAbsentIsNoGPU(t *testing.T) {
	gpus, err := NvidiaSMI{Run: func(context.Context) ([]byte, error) { return nil, errNoDevice }}.GPUs(context.Background())
	if err != nil || len(gpus) != 0 {
		t.Fatalf("%v %v", gpus, err)
	}
	_, err = NvidiaSMI{Run: func(context.Context) ([]byte, error) { return nil, errors.New("driver mismatch") }}.GPUs(context.Background())
	if err == nil {
		t.Fatal("expected error")
	}
}

func TestParseLUIDInstanceAndBuildGPUs(t *testing.T) {
	l, ok := parseLUIDInstance("luid_0x00000000_0x0000C3F1_phys_0")
	if !ok || l != (LUID{High: 0, Low: 0xC3F1}) {
		t.Fatalf("%+v %v", l, ok)
	}
	if _, ok := parseLUIDInstance("_Total"); ok {
		t.Fatal("total accepted")
	}
	dgpu := LUID{Low: 0xC3F1}
	igpu := LUID{Low: 0x1234}
	adapters := []adapter{
		{Name: "AMD Radeon RX 9070 XT", VendorID: 0x1002, DedicatedMB: 16304, SharedMB: 16000, LUID: dgpu},
		{Name: "Microsoft Basic Render Driver", VendorID: 0x1414, Software: true},
		{Name: "Intel(R) Iris(R) Xe Graphics", VendorID: 0x8086, DedicatedMB: 128, SharedMB: 16305, LUID: igpu},
	}
	ded := sumByLUID(map[string]int64{
		"luid_0x00000000_0x0000C3F1_phys_0": 900 << 20,
		"luid_0x00000000_0x0000C3F1_phys_1": 100 << 20,
		"luid_0x00000000_0x00001234_phys_0": 50 << 20,
	})
	sh := sumByLUID(map[string]int64{"luid_0x00000000_0x00001234_phys_0": 1500 << 20})
	gpus, luids := buildGPUs(adapters, ded, sh)
	if len(gpus) != 2 || len(luids) != 2 || luids[0] != dgpu || luids[1] != igpu {
		t.Fatalf("%+v %+v", gpus, luids)
	}
	if g := gpus[0]; g.Vendor != VendorAMD || g.VRAMUsedMB != 1000 || g.Integrated || g.SharedTotalMB != 0 {
		t.Fatalf("dgpu %+v", g)
	}
	if g := gpus[1]; g.Index != 1 || g.Vendor != VendorIntel || !g.Integrated || g.SharedTotalMB != 16305 || g.SharedUsedMB != 1500 || g.VRAMUsedMB != 50 {
		t.Fatalf("igpu %+v", g)
	}
}

func TestParseProcessInstance(t *testing.T) {
	pi, ok := parseProcessInstance("pid_7284_luid_0x00000000_0x0000D1A5_phys_0_eng_3_engtype_3D")
	if !ok || pi.PID != 7284 || pi.LUID != (LUID{High: 0, Low: 0xD1A5}) || pi.Engine != "eng_3" || pi.EngType != "3D" {
		t.Fatalf("%+v %v", pi, ok)
	}
	pi, ok = parseProcessInstance("pid_12_luid_0x00000001_0x0000C3F1_phys_0")
	if !ok || pi.PID != 12 || pi.LUID.High != 1 || pi.Engine != "" {
		t.Fatalf("%+v %v", pi, ok)
	}
	pi, _ = parseProcessInstance("pid_5_luid_0x0_0x1_phys_0_eng_7_engtype_Compute_1")
	if pi.EngType != "Compute_1" {
		t.Fatalf("engtype %q", pi.EngType)
	}
	for _, bad := range []string{"", "luid_0x0_0x1_phys_0", "pid_x_luid_0x0_0x1", "pid_1_foo_0x0_0x1_phys_0", "pid_1_luid_zz_0x1_phys_0"} {
		if _, ok := parseProcessInstance(bad); ok {
			t.Errorf("accepted %q", bad)
		}
	}
}

func TestOwnMemoryAndEngineUtilByLUID(t *testing.T) {
	a := LUID{Low: 0xA}
	mem := map[string]float64{
		"pid_100_luid_0x00000000_0x0000000A_phys_0": 5 << 30, // ours
		"pid_101_luid_0x00000000_0x0000000A_phys_0": 1 << 30, // ours, second model
		"pid_200_luid_0x00000000_0x0000000A_phys_0": 9 << 30, // a game
		"garbage": 1,
	}
	own := ownMemoryByLUID(mem, pidSet([]int{100, 101}))
	if own[a] != 6<<30 {
		t.Fatalf("own %d", own[a])
	}
	util := map[string]float64{
		"pid_100_luid_0x00000000_0x0000000A_phys_0_eng_0_engtype_3D":        10, // ours on 3D
		"pid_200_luid_0x00000000_0x0000000A_phys_0_eng_0_engtype_3D":        60, // game on 3D
		"pid_200_luid_0x00000000_0x0000000A_phys_0_eng_1_engtype_Copy":      5,
		"pid_100_luid_0x00000000_0x0000000A_phys_0_eng_2_engtype_Compute_0": 80, // ours on compute
		"pid_300_luid_0x00000000_0x0000000A_phys_0_eng_2_engtype_Compute_0": 30,
	}
	total, foreign := engineUtilByLUID(util, pidSet([]int{100}))
	// busiest engine overall: compute 80+30 = 110 -> capped at 100
	if total[a] != 100 {
		t.Fatalf("total %v", total[a])
	}
	// without pid 100: 3D 60, copy 5, compute 30 -> 60
	if foreign[a] != 60 {
		t.Fatalf("foreign %v", foreign[a])
	}
}

func TestNvidiaSMISample(t *testing.T) {
	calls := 0
	smi := NvidiaSMI{Exec: func(_ context.Context, args []string) ([]byte, error) {
		calls++
		if strings.HasPrefix(args[0], "--query-gpu") {
			return []byte("0, NVIDIA GeForce RTX 2080 SUPER, 8192, 7000, 55, GPU-aaa\n1, NVIDIA GeForce RTX 3070, 8192, 300, [N/A], GPU-bbb\n"), nil
		}
		return []byte("GPU-aaa, 4242, 5000\nGPU-aaa, 777, 1500\nGPU-bbb, 99, [N/A]\n"), nil
	}}
	gpus, usage, err := smi.Sample(context.Background(), []int{4242})
	if err != nil || len(gpus) != 2 || len(usage) != 2 || calls != 2 {
		t.Fatalf("%v %+v %+v", err, gpus, usage)
	}
	if u := usage[0]; !u.OwnKnown || u.OwnDedicatedMB != 5000 || !u.UtilKnown || u.UtilPercent != 55 || u.ForeignUtilKnown {
		t.Fatalf("gpu0 %+v", u)
	}
	if u := usage[1]; !u.OwnKnown || u.OwnDedicatedMB != 0 || u.UtilKnown {
		t.Fatalf("gpu1 %+v", u)
	}
	empty, err := parseNvidiaSMIApps([]byte("No running processes found\n"))
	if err != nil || len(empty) != 0 {
		t.Fatalf("%v %v", empty, err)
	}
}

func TestForeignMaxByLUID(t *testing.T) {
	a, b := LUID{Low: 0xA}, LUID{Low: 0xB}
	shared := map[string]float64{
		"pid_100_luid_0x00000000_0x0000000A_phys_0": 2 << 30, // ours
		"pid_200_luid_0x00000000_0x0000000A_phys_0": 5 << 30, // LM Studio, two instances
		"pid_200_luid_0x00000000_0x0000000A_phys_1": 2 << 30,
		"pid_300_luid_0x00000000_0x0000000A_phys_0": 1 << 30, // a browser
		"pid_300_luid_0x00000000_0x0000000B_phys_0": 3 << 30, // other adapter
		"garbage": 9 << 30,
	}
	got := foreignMaxByLUID(shared, pidSet([]int{100}))
	if got[a] != 7<<30 || got[b] != 3<<30 || len(got) != 2 {
		t.Fatalf("%v", got)
	}
}
