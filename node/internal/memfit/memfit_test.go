package memfit

import (
	"strings"
	"testing"

	"topcoat/node/internal/gguf"
)

// A 30B-A3B Q4_K_M like the one on the laptop: 18.6 GB file, 48 layers, 4 KV heads of 128.
var moe30B = gguf.Meta{Architecture: "qwen3moe", BlockCount: 48, EmbeddingLength: 2048, HeadCount: 32, HeadCountKV: 4, KeyLength: 128, ValueLength: 128}

const gb = 1 << 30

func TestCPUAndIntegratedGPULoadsCountEverythingAsSystemMemory(t *testing.T) {
	cpu := Estimate(Model{FileBytes: 18*gb + gb/2, Meta: moe30B, Context: 8192})
	// KV: 48 * 4 * 256 * 2 bytes * 8192 = 768 MiB
	if cpu.KVMB != 768 {
		t.Fatalf("kv %d", cpu.KVMB)
	}
	if cpu.SystemMB != cpu.WeightsMB+cpu.KVMB+cpu.OverheadMB || cpu.SystemMB < 19000 || cpu.SystemMB > 21000 {
		t.Fatalf("%+v", cpu)
	}
	igpu := Estimate(Model{FileBytes: 18*gb + gb/2, Meta: moe30B, Context: 8192, GPU: true, Integrated: true, FreeVRAMMB: 16000})
	if igpu.SystemMB != cpu.SystemMB {
		t.Fatalf("an iGPU uses system memory: %+v vs %+v", igpu, cpu)
	}
}

func TestDiscreteGPUTakesWhatFitsIntoVRAM(t *testing.T) {
	m := Model{FileBytes: 4 * gb, Meta: gguf.Meta{BlockCount: 32, EmbeddingLength: 4096, HeadCount: 32, HeadCountKV: 8}, Context: 4096, GPU: true, FreeVRAMMB: 7000}
	n := Estimate(m)
	if n.SystemMB != 512 {
		t.Fatalf("fits into VRAM: only host buffers in RAM, got %+v", n)
	}
	m.FreeVRAMMB = 2000 // 7 GB model on an 8 GB card that is busy: the rest lives in RAM
	n = Estimate(m)
	if n.SystemMB <= 512 || n.SystemMB > n.WeightsMB+n.KVMB+n.OverheadMB {
		t.Fatalf("%+v", n)
	}
}

func TestCheckRefusesWhatWouldSwap(t *testing.T) {
	m := Model{FileBytes: 18*gb + gb/2, Meta: moe30B, Context: 8192}
	// laptop with 32 GB of which 13 GB are in use by other programs: 19 GB free
	v := Check(m, 19*1024, 1024)
	if v.OK {
		t.Fatalf("a 30B model needing ~%d MB fits into 19 GB? %+v", v.Need.SystemMB, v)
	}
	if msg := v.Message(); !strings.Contains(msg, "not enough free RAM") || !strings.Contains(msg, "19456 MB are free") {
		t.Fatal(msg)
	}
	if v := Check(m, 26*1024, 1024); !v.OK {
		t.Fatalf("26 GB free must be enough: %+v", v)
	}
	// without metadata the KV cache is guessed, never zero
	blind := Estimate(Model{FileBytes: 4 * gb, Context: 8192})
	if blind.KVMB <= 0 {
		t.Fatalf("%+v", blind)
	}
}

func TestAutoReserve(t *testing.T) {
	for _, c := range []struct{ total, want int64 }{{8192, 4096}, {32768, 4096}, {65536, 7864}, {131072, 15728}} {
		if got := AutoReserveMB(c.total); got != c.want {
			t.Errorf("%d MB: reserve %d, want %d", c.total, got, c.want)
		}
	}
}

// A Gemma-like model with sliding-window attention (the dimensions here are illustrative): on the
// maintainer's node a 12B with a
// 16384 context was refused for "KV cache 24576 MB" although it really used 7.5 GB of VRAM and
// 6.8 GB of RAM. Only the full-attention layers (every 6th) grow with the context.
func TestSlidingWindowKVCacheIsNotCountedAtFullLength(t *testing.T) {
	meta := gguf.Meta{
		Architecture: "gemmax", BlockCount: 48, EmbeddingLength: 4096, HeadCount: 16, HeadCountKV: 16,
		KeyLength: 512, ValueLength: 512, SlidingWindow: 1024, SWAPattern: 6, KeyLengthSWA: 256, ValueLengthSWA: 256,
	}
	m := Model{FileBytes: 7 << 30, Meta: meta, Context: 16384, Slots: 1, GPU: true, FreeVRAMMB: 14000}
	got := Estimate(m)
	naive := gguf.Meta{BlockCount: 48, EmbeddingLength: 4096, HeadCount: 16, HeadCountKV: 16, KeyLength: 512, ValueLength: 512}
	naiveMB := naive.KVCacheBytes(16384) >> 20
	if naiveMB < 24000 {
		t.Fatalf("the naive figure should be the refused 24 GB-class one, got %d MB", naiveMB)
	}
	if got.KVMB*3 > naiveMB {
		t.Fatalf("the KV estimate %d MB is not clearly below the full-length figure %d MB", got.KVMB, naiveMB)
	}
	// with the 8 full layers at 16384 and 40 sliding ones at 1024+512 the cache is about 4.4 GB
	if got.KVMB < 3500 || got.KVMB > 5500 {
		t.Fatalf("KV %d MB", got.KVMB)
	}
	// and the load that was refused passes the check on a machine with room for 7 GB
	if v := Check(m, 14000, 4096); !v.OK {
		t.Fatalf("refused: %s", v.Message())
	}
}

// gemma-4-12B-it-QAT-Q4_0 with the shapes read from the real GGUF: 48 layers, head_count_kv per
// layer (8 for the 40 sliding layers, 1 for the 8 full ones at positions 5, 11, …), key/value
// length 512 (full) and 256 (sliding), window 1024. At a context of 16384 the old estimate was
// "KV cache 24576 MB" and the load was refused with insufficient_memory although the model really
// takes 7.5 GB of VRAM and 6.8 GB of RAM.
func gemma4Meta() gguf.Meta {
	m := gguf.Meta{
		Architecture: "gemma4", BlockCount: 48, EmbeddingLength: 3840, HeadCount: 16,
		KeyLength: 512, ValueLength: 512, KeyLengthSWA: 256, ValueLengthSWA: 256, SlidingWindow: 1024,
		ContextLength: 262144,
	}
	for i := 0; i < 48; i++ {
		full := i%6 == 5
		m.SWALayers = append(m.SWALayers, !full)
		kv := 8
		if full {
			kv = 1
		}
		m.HeadCountKVLayers = append(m.HeadCountKVLayers, kv)
	}
	return m
}

func TestGemma4KVCacheAt16384(t *testing.T) {
	// per layer: kv heads × (key + value) × 2 bytes × cells
	fullBytes := int64(8) * 1 * (512 + 512) * 2 * 16384          // 8 layers, whole context
	slideBytes := int64(40) * 8 * (256 + 256) * 2 * (1024 + 512) // 40 layers, window + micro-batch
	wantMB := (fullBytes + slideBytes + mb - 1) / mb
	m := Model{FileBytes: 7 << 30, Meta: gemma4Meta(), Context: 16384, Slots: 1, GPU: true, FreeVRAMMB: 14000}
	got := Estimate(m)
	if got.KVMB != wantMB {
		t.Fatalf("KV %d MB, want %d MB", got.KVMB, wantMB)
	}
	if got.KVMB != 736 { // 256 MiB for the 8 full layers + 480 MiB for the 40 sliding ones
		t.Fatalf("KV %d MB (the hand calculation says 736)", got.KVMB)
	}
	// CPU backend on a machine with 16 GB free and the usual reserve: fits now; with the old
	// 24576 MB it could not
	cpu := Model{FileBytes: 7 << 30, Meta: gemma4Meta(), Context: 16384, Slots: 1}
	if v := Check(cpu, 16000, 4096); !v.OK {
		t.Fatalf("refused: %s", v.Message())
	}
	oldMeta := gemma4Meta()
	oldMeta.SWALayers, oldMeta.HeadCountKVLayers, oldMeta.HeadCountKV = nil, nil, 0
	oldMeta.SlidingWindow, oldMeta.KeyLengthSWA, oldMeta.ValueLengthSWA = 0, 0, 0
	old := Model{FileBytes: 7 << 30, Meta: oldMeta, Context: 16384, GPU: true, FreeVRAMMB: 14000}
	if Estimate(old).KVMB != 24576 {
		t.Fatalf("the old figure was %d MB", Estimate(old).KVMB)
	}
	if v := Check(Model{FileBytes: 7 << 30, Meta: oldMeta, Context: 16384}, 16000, 4096); v.OK {
		t.Fatal("the old estimate should have refused this load")
	}
	// two slots of 16384: the sliding layers keep their window once per sequence
	two := Estimate(Model{FileBytes: 7 << 30, Meta: gemma4Meta(), Context: 32768, Slots: 2, GPU: true, FreeVRAMMB: 14000})
	wantTwo := (int64(8)*1*(512+512)*2*32768 + int64(40)*8*(256+256)*2*(2*1024+512) + mb - 1) / mb
	if two.KVMB != wantTwo {
		t.Fatalf("two slots: %d MB, want %d", two.KVMB, wantTwo)
	}
}

// a 12B-class model with a sliding-window-free layout: 8 GB weights, 40 layers, 8 KV heads of 128
var dense12B = gguf.Meta{BlockCount: 40, EmbeddingLength: 5120, HeadCount: 40, HeadCountKV: 8, KeyLength: 128, ValueLength: 128, ContextLength: 131072}

func TestAutoContextOnACPUGrowsToTheCapWhenTheMemoryHolds(t *testing.T) {
	r := AutoContext(AutoInput{
		Model: Model{FileBytes: 18*gb + gb/2, Meta: moe30B},
		Min:   8192, FreeRAMMB: 28 * 1024, ReserveMB: 4096,
	})
	if r.Context != DefaultMaxAutoContext || r.OnDevice {
		t.Fatalf("plenty of room: the cap, got %+v", r)
	}
	// 22 GB free with a 4 GB reserve: the 30B model's 19 GB leave room for a little KV only
	r = AutoContext(AutoInput{
		Model: Model{FileBytes: 18*gb + gb/2, Meta: moe30B},
		Min:   8192, FreeRAMMB: 24 * 1024, ReserveMB: 4096,
	})
	if r.Context < 8192 || r.Context >= DefaultMaxAutoContext {
		t.Fatalf("tight: more than the floor, less than the cap, got %+v", r)
	}
	// not even the floor: the floor comes back, the guard of the runner reports the shortage
	r = AutoContext(AutoInput{
		Model: Model{FileBytes: 18*gb + gb/2, Meta: moe30B},
		Min:   16384, FreeRAMMB: 16 * 1024, ReserveMB: 4096,
	})
	if r.Context != 16384 || !strings.Contains(r.Reason, "not even the floor") {
		t.Fatalf("got %+v", r)
	}
}

func TestAutoContextOnADiscreteGPUKeepsTheModelOnTheDevice(t *testing.T) {
	in := AutoInput{
		Model: Model{FileBytes: 8 * gb, Meta: dense12B, GPU: true, FreeVRAMMB: 15000},
		Min:   8192, FreeRAMMB: 20000, ReserveMB: 4096, VRAMMarginMB: 1024,
	}
	r := AutoContext(in)
	if !r.OnDevice || r.Context <= 8192 {
		t.Fatalf("room on the card: grow while the model stays on it, got %+v", r)
	}
	// the chosen value fits the device, the next step up does not (or is the cap)
	m := in.Model
	m.Context = r.Context
	n := Estimate(m)
	if n.WeightsMB+n.KVMB+n.OverheadMB+in.VRAMMarginMB > in.Model.FreeVRAMMB {
		t.Fatalf("does not fit the device: %+v", n)
	}
}

func TestAutoContextOnADiscreteGPUMayKeepPartOfTheModelInRAM(t *testing.T) {
	// the model does not fit the card at all: the context still grows as far as the free RAM
	// holds the part that spills over, the way a model on the CPU would grow
	in := AutoInput{
		Model: Model{FileBytes: 8 * gb, Meta: dense12B, GPU: true, FreeVRAMMB: 4000},
		Min:   8192, FreeRAMMB: 30000, ReserveMB: 4096, VRAMMarginMB: 1024,
	}
	r := AutoContext(in)
	if r.OnDevice || r.Context <= 8192 {
		t.Fatalf("spill into RAM: grow beyond the floor, not on the device, got %+v", r)
	}
	m := in.Model
	m.Context = r.Context
	if n := Estimate(m); n.SystemMB+in.ReserveMB > in.FreeRAMMB {
		t.Fatalf("the chosen context must fit the free RAM: %+v", n)
	}
	// RAM too small even for the floor: the floor comes back and the guard reports the shortage
	in.FreeRAMMB = 6000
	r = AutoContext(in)
	if r.Context != 8192 || !strings.Contains(r.Reason, "not even the floor") {
		t.Fatalf("no RAM for the spill: the floor, got %+v", r)
	}
}

func TestAutoContextNeverExceedsTheTrainedLengthOrTheConfiguredCap(t *testing.T) {
	small := gguf.Meta{BlockCount: 8, EmbeddingLength: 512, HeadCount: 8, HeadCountKV: 2, ContextLength: 4096}
	r := AutoContext(AutoInput{Model: Model{FileBytes: gb / 4, Meta: small}, Min: 2048, FreeRAMMB: 30000, ReserveMB: 4096})
	if r.Context != 4096 {
		t.Fatalf("trained for 4096: got %+v", r)
	}
	r = AutoContext(AutoInput{Model: Model{FileBytes: gb / 4, Meta: gguf.Meta{ContextLength: 131072, BlockCount: 8, EmbeddingLength: 512, HeadCount: 8, HeadCountKV: 2}}, Min: 2048, Max: 16384, FreeRAMMB: 30000, ReserveMB: 4096})
	if r.Context != 16384 {
		t.Fatalf("configured cap 16384: got %+v", r)
	}
	// a floor above the cap wins: the caller needs it
	r = AutoContext(AutoInput{Model: Model{FileBytes: gb / 4, Meta: small}, Min: 8192, FreeRAMMB: 30000, ReserveMB: 4096})
	if r.Context != 8192 {
		t.Fatalf("floor above the trained length: got %+v", r)
	}
}

func TestAutoContextCountsAllSlots(t *testing.T) {
	one := AutoContext(AutoInput{Model: Model{FileBytes: 4 * gb, Meta: dense12B, Slots: 1}, Min: 4096, FreeRAMMB: 12000, ReserveMB: 2048})
	two := AutoContext(AutoInput{Model: Model{FileBytes: 4 * gb, Meta: dense12B, Slots: 2}, Min: 4096, FreeRAMMB: 12000, ReserveMB: 2048})
	if two.Context > one.Context {
		t.Fatalf("two slots share the memory: %d per slot vs %d with one", two.Context, one.Context)
	}
}
