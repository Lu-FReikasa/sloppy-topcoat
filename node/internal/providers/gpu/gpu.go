// Package gpu defines the GPUProvider interface and the per-OS system provider.
package gpu

import (
	"context"
	"errors"
	"strings"
)

// Vendors (common.schema.json#/$defs/gpu.vendor).
const (
	VendorNVIDIA = "nvidia"
	VendorAMD    = "amd"
	VendorIntel  = "intel"
	VendorOther  = "other"
)

// GPU is one GPU with its VRAM totals.
type GPU struct {
	Index       int
	Name        string
	Vendor      string
	VRAMTotalMB int64
	VRAMUsedMB  int64
	// Integrated GPUs (iGPU/APU) use system memory; it is reported
	// separately from the (small) dedicated carve-out.
	Integrated    bool
	SharedTotalMB int64
	SharedUsedMB  int64
}

// GPUProvider lists GPUs with VRAM total/used. The name is fixed by
// docs/ARCHITECTURE.md, hence the stutter.
type GPUProvider interface { //nolint:revive // name mandated by docs/ARCHITECTURE.md
	GPUs(ctx context.Context) ([]GPU, error)
}

// errNoDevice means the vendor library / tool is absent: no such GPU.
var errNoDevice = errors.New("no device")

// Multi combines several providers (e.g. NVML + an AMD backend). GPUs are
// re-indexed in order. Errors of individual backends are joined and returned
// together with whatever the backends found; a backend that reports GPUs and
// an error (e.g. DXGI adapters without the PDH usage counters) keeps its GPUs.
type Multi []GPUProvider

// GPUs implements GPUProvider.
func (m Multi) GPUs(ctx context.Context) ([]GPU, error) {
	var out []GPU
	var errs []error
	for _, p := range m {
		gpus, err := p.GPUs(ctx)
		if err != nil {
			errs = append(errs, err)
		}
		for _, g := range gpus {
			g.Index = len(out)
			out = append(out, g)
		}
	}
	return out, errors.Join(errs...)
}

// ProcessUsage splits one GPU's usage into the node's own model processes and
// everything else (E1-06 VRAM pressure). The Known flags are false where the
// platform cannot attribute usage to processes.
type ProcessUsage struct {
	// OwnDedicatedMB is the dedicated VRAM of the given processes.
	OwnDedicatedMB int64
	OwnKnown       bool
	// OwnSharedMB is the shared (system) memory of the given processes; it
	// rises when the driver pages our buffers out of VRAM (demotion).
	OwnSharedMB int64
	SharedKnown bool
	// UtilPercent is the utilization of the whole GPU; ForeignUtilPercent the
	// part not caused by the given processes.
	UtilPercent        float64
	UtilKnown          bool
	ForeignUtilPercent float64
	ForeignUtilKnown   bool
	// ForeignSharedMaxMB is the largest shared (system) memory use of a single
	// other process on this GPU: an application that did not get enough VRAM
	// and was placed in system memory instead ("unmet demand", E1-06).
	ForeignSharedMaxMB int64
	ForeignSharedKnown bool
}

// Sampler is implemented by providers that can attribute usage to processes.
// The two slices are index-aligned.
type Sampler interface {
	Sample(ctx context.Context, pids []int) ([]GPU, []ProcessUsage, error)
}

// SampleOf returns the GPUs of p plus per-process usage where p supports it
// (unknown usage otherwise). pids are the node's model processes.
func SampleOf(ctx context.Context, p GPUProvider, pids []int) ([]GPU, []ProcessUsage, error) {
	if s, ok := p.(Sampler); ok {
		return s.Sample(ctx, pids)
	}
	gpus, err := p.GPUs(ctx)
	return gpus, make([]ProcessUsage, len(gpus)), err
}

// Sample implements Sampler; members without process attribution report
// unknown usage. Members that fail are skipped like in GPUs.
func (m Multi) Sample(ctx context.Context, pids []int) ([]GPU, []ProcessUsage, error) {
	var gpus []GPU
	var usage []ProcessUsage
	var errs []error
	for _, p := range m {
		g, u, err := SampleOf(ctx, p, pids)
		if err != nil {
			errs = append(errs, err)
		}
		for i := range g {
			g[i].Index = len(gpus)
			gpus = append(gpus, g[i])
			if i < len(u) {
				usage = append(usage, u[i])
			} else {
				usage = append(usage, ProcessUsage{})
			}
		}
	}
	return gpus, usage, errors.Join(errs...)
}

// pidSet turns pids into a lookup set.
func pidSet(pids []int) map[int]bool {
	s := make(map[int]bool, len(pids))
	for _, p := range pids {
		if p > 0 {
			s[p] = true
		}
	}
	return s
}

// Diagnose explains an incomplete GPU detection for a WARN log line, or
// returns "" when detection worked. Missing counters used to fall back to the
// CPU backend silently (test 1, A7).
func Diagnose(err error) string {
	if err == nil {
		return ""
	}
	if strings.Contains(err.Error(), "Pdh") || strings.Contains(err.Error(), "counters") {
		return "GPU memory counters are not readable, so VRAM usage is unknown; a Windows service account " +
			"needs to be in the group \"Performance Monitor Users\" (S-1-5-32-558), which `topcoat-node install` adds"
	}
	return "GPU detection incomplete"
}
