// Package memfit estimates what loading a model costs in system memory and decides whether
// the node has room for it (D27): it refuses a load that would push the machine into swap.
package memfit

import (
	"fmt"

	"topcoat/node/internal/gguf"
)

const mb = 1 << 20

// Model describes one load.
type Model struct {
	// FileBytes is the size of the GGUF file (the weights).
	FileBytes int64
	// Meta is the GGUF metadata; zero if it could not be read (the KV cache is then guessed).
	Meta gguf.Meta
	// Context is the context length (-c), summed over all slots.
	Context int
	// Slots is how many parallel sequences share it (0 = 1); sliding-window layers keep their
	// window once per sequence.
	Slots int
	// GPU is true when layers may be offloaded to a GPU device (not the cpu backend).
	GPU bool
	// Integrated is true when that GPU uses system memory (iGPU / APU): offloaded layers
	// are system memory too.
	Integrated bool
	// FreeVRAMMB is the free dedicated GPU memory of the device the model goes to.
	FreeVRAMMB int64
}

// AutoReserveMB is the RAM that stays free after a load when the config names no reserve: the
// larger of 4 GB and 12 % of the machine's RAM. Measured on a 32 GB laptop: a 7B on the iGPU
// (5.2 GB shared) plus a 30B on the CPU (18.7 GB) left 1.6 GB free, and the desktop of the
// person using the machine needs more than that.
func AutoReserveMB(totalMB int64) int64 {
	return max(4096, totalMB*12/100)
}

// Need is the estimated memory of a load, in MB.
type Need struct {
	WeightsMB  int64
	KVMB       int64
	OverheadMB int64
	// SystemMB is the part that lands in system RAM (all of it on the CPU backend and on an
	// iGPU, only what does not fit into the free VRAM on a discrete GPU).
	SystemMB int64
}

// Estimate computes what the load needs. The overhead (compute buffers, tokenizer, the
// server itself) is a heuristic: 300 MB plus 2 % of the weights, checked against a
// llama.cpp log (compute buffer of 300 MiB for a 7B on CUDA).
func Estimate(m Model) Need {
	n := Need{WeightsMB: (m.FileBytes + mb - 1) / mb}
	if kv := m.Meta.KVCacheBytesSlots(m.Context, m.Slots); kv > 0 {
		n.KVMB = (kv + mb - 1) / mb
	} else {
		// no metadata: about 1 MB per 1000 tokens per GB of weights is a fair guess for
		// grouped-query models; better too high than swap
		n.KVMB = int64(m.Context) * max(n.WeightsMB/1024, 1) / 1000
	}
	n.OverheadMB = 300 + n.WeightsMB/50
	total := n.WeightsMB + n.KVMB + n.OverheadMB
	switch {
	case !m.GPU || m.Integrated:
		n.SystemMB = total
	default:
		// a discrete GPU takes what fits; host buffers and staging always need some RAM
		n.SystemMB = max(total-m.FreeVRAMMB, 0) + 512
		n.SystemMB = min(n.SystemMB, total)
	}
	return n
}

// Verdict is the outcome of a memory check.
type Verdict struct {
	OK        bool
	Need      Need
	FreeMB    int64
	ReserveMB int64
}

// Message describes a shortfall for humans and the model runner error.
func (v Verdict) Message() string {
	return fmt.Sprintf("not enough free RAM: the model needs about %d MB (weights %d, KV cache %d, overhead %d) "+
		"and %d MB must stay free, but only %d MB are free",
		v.Need.SystemMB, v.Need.WeightsMB, v.Need.KVMB, v.Need.OverheadMB, v.ReserveMB, v.FreeMB)
}

// Check compares the estimate with the free RAM.
func Check(m Model, freeMB, reserveMB int64) Verdict {
	n := Estimate(m)
	return Verdict{OK: n.SystemMB+reserveMB <= freeMB, Need: n, FreeMB: freeMB, ReserveMB: reserveMB}
}

// contextLadder is the context lengths per slot the node chooses from when a load asks for "at
// least" some context. Steps of 1.5x and 2x keep the number of candidates small and the values
// round.
var contextLadder = []int{2048, 3072, 4096, 6144, 8192, 12288, 16384, 24576, 32768, 49152, 65536, 98304, 131072, 196608, 262144}

// DefaultMaxAutoContext is the largest context per slot AutoContext picks when the config names no
// cap: past it the prompt processing time, not the memory, is what the person waits for.
const DefaultMaxAutoContext = 32768

// AutoInput describes a load for AutoContext.
type AutoInput struct {
	// Model: FileBytes, Meta, Slots, GPU, Integrated and FreeVRAMMB are used; Context is ignored.
	Model Model
	// Min is the floor per slot: what the caller needs and what the node would have used anyway.
	Min int
	// Max is the cap per slot (0 = DefaultMaxAutoContext). The model's trained length caps it too.
	Max int
	// FreeRAMMB and ReserveMB are the free system memory and what must stay free after the load.
	FreeRAMMB, ReserveMB int64
	// VRAMMarginMB is the device memory that stays free on a discrete GPU (llama.cpp's fit margin).
	VRAMMarginMB int64
}

// AutoResult is the choice: the context per slot and why.
type AutoResult struct {
	Context int
	// OnDevice is true when the whole model with this context stays on the discrete GPU.
	OnDevice bool
	Reason   string
}

// AutoContext picks the largest context per slot from the ladder that the machine holds.
//
//   - Discrete GPU, and the model with the floor context fits on the device: the largest context
//     that still fits on the device with the margin (a context that pushes layers into system
//     memory makes every token slower, which is worse than less context).
//   - Discrete GPU, and the model spills over already at the floor: the floor. Growing would only
//     move more of it off the device.
//   - CPU or integrated GPU: the largest context whose system memory need plus the reserve fits
//     into the free memory.
//
// When not even the floor fits, the floor is returned and the memory guard says so.
func AutoContext(in AutoInput) AutoResult {
	slots := max(in.Model.Slots, 1)
	limit := in.Max
	if limit <= 0 {
		limit = DefaultMaxAutoContext
	}
	if trained := in.Model.Meta.ContextLength; trained > 0 && trained < limit {
		limit = trained
	}
	floor := max(in.Min, 1)
	if limit < floor {
		limit = floor
	}
	steps := []int{floor}
	for _, v := range contextLadder {
		if v > floor && v < limit {
			steps = append(steps, v)
		}
	}
	if limit > floor {
		steps = append(steps, limit)
	}

	eval := func(ctx int) (ramOK, onDevice bool) {
		m := in.Model
		m.Context = ctx * slots
		need := Estimate(m)
		ramOK = need.SystemMB+in.ReserveMB <= in.FreeRAMMB
		onDevice = m.GPU && !m.Integrated && m.FreeVRAMMB > 0 &&
			need.WeightsMB+need.KVMB+need.OverheadMB+in.VRAMMarginMB <= m.FreeVRAMMB
		return ramOK, onDevice
	}

	// A discrete GPU holds what fits and the rest of the model runs from RAM (the runner fits the
	// layers to the device, see Estimate). So the context grows as far as the free RAM holds
	// the spill-over, and a model that does not fit the card at all is not a reason to stay small.
	discrete := in.Model.GPU && !in.Model.Integrated && in.Model.FreeVRAMMB > 0
	for i := len(steps) - 1; i >= 0; i-- {
		ramOK, onDevice := eval(steps[i])
		if !ramOK {
			continue
		}
		reason := "the largest context the free memory holds"
		if discrete && onDevice {
			reason = "the largest context that keeps the whole model on the device"
		}
		if discrete && !onDevice {
			reason = "the largest context the free RAM holds for the part of the model the device does not take"
		}
		if steps[i] == limit {
			reason = "the cap (config or the model's trained length)"
		}
		return AutoResult{Context: steps[i], OnDevice: onDevice, Reason: reason}
	}
	return AutoResult{Context: floor, Reason: "not even the floor fits the free memory"}
}
