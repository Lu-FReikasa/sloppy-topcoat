package gpu

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"sync"
	"time"

	"github.com/ebitengine/purego"
)

// NVML reads NVIDIA GPUs through libnvidia-ml.so.1, loaded at runtime with
// purego (no CGO). If the library is missing there is no NVIDIA driver, which
// is not an error. If loading fails otherwise, nvidia-smi is used.
//
// Note: purego makes the Linux binary dynamically linked against glibc
// (libc/libdl); it still builds with CGO_ENABLED=0.
//
// UNVERIFIED on real hardware.
type NVML struct {
	Fallback GPUProvider
}

type nvmlMemory struct {
	Total, Free, Used uint64
}

type nvmlLib struct {
	init   func() int32
	count  func(*uint32) int32
	handle func(uint32, *uintptr) int32
	name   func(uintptr, *byte, uint32) int32
	memory func(uintptr, *nvmlMemory) int32
	// Optional (E1-06), nil when the driver lacks them.
	computeProcs  func(uintptr, *uint32, *byte) int32
	graphicsProcs func(uintptr, *uint32, *byte) int32
	utilization   func(uintptr, *nvmlUtilization) int32
	processUtil   func(uintptr, *byte, *uint32, uint64) int32
}

type nvmlUtilization struct {
	GPU, Memory uint32
}

// NVML return codes used here.
const (
	nvmlSuccess          = 0
	nvmlInsufficientSize = 7
	nvmlNotFound         = 6
)

// Struct strides of nvmlProcessInfo_t (v2/v3 layout: pid, pad, usedGpuMemory,
// gpuInstanceId, computeInstanceId) and nvmlProcessUtilizationSample_t (pid,
// pad, timeStamp, smUtil, memUtil, encUtil, decUtil). Buffers are allocated
// with nvmlSlotBytes per entry so that a larger struct in some driver version
// can never write past them; values would then be wrong, not memory corrupt.
const (
	nvmlProcInfoSize   = 24
	nvmlUtilSampleSize = 32
	nvmlSlotBytes      = 64
)

var (
	nvmlOnce sync.Once
	nvml     *nvmlLib
	nvmlErr  error
)

func loadNVML() (*nvmlLib, error) {
	nvmlOnce.Do(func() {
		h, err := purego.Dlopen("libnvidia-ml.so.1", purego.RTLD_NOW|purego.RTLD_GLOBAL)
		if err != nil {
			nvmlErr = errNoDevice
			return
		}
		l := &nvmlLib{}
		defer func() {
			if r := recover(); r != nil { // missing symbol
				nvmlErr = fmt.Errorf("nvml: %v", r)
				nvml = nil
			}
		}()
		purego.RegisterLibFunc(&l.init, h, "nvmlInit_v2")
		purego.RegisterLibFunc(&l.count, h, "nvmlDeviceGetCount_v2")
		purego.RegisterLibFunc(&l.handle, h, "nvmlDeviceGetHandleByIndex_v2")
		purego.RegisterLibFunc(&l.name, h, "nvmlDeviceGetName")
		purego.RegisterLibFunc(&l.memory, h, "nvmlDeviceGetMemoryInfo")
		optional(h, &l.computeProcs, "nvmlDeviceGetComputeRunningProcesses_v3")
		optional(h, &l.graphicsProcs, "nvmlDeviceGetGraphicsRunningProcesses_v3")
		optional(h, &l.utilization, "nvmlDeviceGetUtilizationRates")
		optional(h, &l.processUtil, "nvmlDeviceGetProcessUtilization")
		if rc := l.init(); rc != 0 {
			nvmlErr = fmt.Errorf("nvmlInit_v2 returned %d", rc)
			return
		}
		nvml = l
	})
	return nvml, nvmlErr
}

// GPUs implements GPUProvider.
func (n NVML) GPUs(ctx context.Context) ([]GPU, error) {
	l, err := loadNVML()
	if errors.Is(err, errNoDevice) {
		return nil, nil // no NVIDIA driver installed
	}
	if err != nil {
		if n.Fallback != nil {
			return n.Fallback.GPUs(ctx)
		}
		return nil, err
	}
	var count uint32
	if rc := l.count(&count); rc != 0 {
		return nil, fmt.Errorf("nvmlDeviceGetCount_v2 returned %d", rc)
	}
	out := make([]GPU, 0, count)
	for i := uint32(0); i < count; i++ {
		var h uintptr
		if rc := l.handle(i, &h); rc != 0 {
			return out, fmt.Errorf("nvmlDeviceGetHandleByIndex_v2(%d) returned %d", i, rc)
		}
		buf := make([]byte, 96)
		name := "NVIDIA GPU"
		if rc := l.name(h, &buf[0], uint32(len(buf))); rc == 0 {
			name = cString(buf)
		}
		var mem nvmlMemory
		if rc := l.memory(h, &mem); rc != 0 {
			return out, fmt.Errorf("nvmlDeviceGetMemoryInfo(%d) returned %d", i, rc)
		}
		out = append(out, GPU{Index: int(i), Name: name, Vendor: VendorNVIDIA,
			VRAMTotalMB: int64(mem.Total >> 20), VRAMUsedMB: int64(mem.Used >> 20)})
	}
	return out, nil
}

// optional registers fn only if the symbol exists.
func optional(h uintptr, fn any, name string) {
	if _, err := purego.Dlsym(h, name); err == nil {
		purego.RegisterLibFunc(fn, h, name)
	}
}

// procMemory returns the used GPU memory (bytes) per pid from one of the
// running-process lists; ok=false if the call is unavailable or fails.
func procMemory(call func(uintptr, *uint32, *byte) int32, dev uintptr) (map[int]uint64, bool) {
	if call == nil {
		return nil, false
	}
	var n uint32
	rc := call(dev, &n, nil)
	if rc == nvmlSuccess {
		return map[int]uint64{}, true
	}
	if rc != nvmlInsufficientSize {
		return nil, false
	}
	n += 8 // processes may start between the two calls
	buf := make([]byte, int(n)*nvmlSlotBytes)
	if rc = call(dev, &n, &buf[0]); rc != nvmlSuccess {
		return nil, false
	}
	return parseProcInfos(buf, int(n)), true
}

// parseProcInfos decodes n nvmlProcessInfo_t entries.
func parseProcInfos(buf []byte, n int) map[int]uint64 {
	out := map[int]uint64{}
	for i := 0; i < n && (i+1)*nvmlProcInfoSize <= len(buf); i++ {
		e := buf[i*nvmlProcInfoSize:]
		pid := int(binary.LittleEndian.Uint32(e[0:4]))
		used := binary.LittleEndian.Uint64(e[8:16])
		if used == math.MaxUint64 { // NVML_VALUE_NOT_AVAILABLE
			continue
		}
		out[pid] = max(out[pid], used)
	}
	return out
}

// procUtil returns the SM utilization per pid since lastSeen (µs).
func procUtil(call func(uintptr, *byte, *uint32, uint64) int32, dev uintptr, lastSeen uint64) (map[int]uint32, bool) {
	if call == nil {
		return nil, false
	}
	var n uint32
	rc := call(dev, nil, &n, lastSeen)
	if rc == nvmlNotFound {
		return map[int]uint32{}, true // no samples: nobody busy
	}
	if rc != nvmlInsufficientSize {
		return nil, false
	}
	n += 8
	buf := make([]byte, int(n)*nvmlSlotBytes)
	if rc = call(dev, &buf[0], &n, lastSeen); rc != nvmlSuccess {
		return nil, false
	}
	return parseUtilSamples(buf, int(n)), true
}

// parseUtilSamples decodes n nvmlProcessUtilizationSample_t entries (max SM
// utilization per pid).
func parseUtilSamples(buf []byte, n int) map[int]uint32 {
	out := map[int]uint32{}
	for i := 0; i < n && (i+1)*nvmlUtilSampleSize <= len(buf); i++ {
		e := buf[i*nvmlUtilSampleSize:]
		pid := int(binary.LittleEndian.Uint32(e[0:4]))
		out[pid] = max(out[pid], binary.LittleEndian.Uint32(e[16:20]))
	}
	return out
}

// Sample implements Sampler. NVIDIA on Linux does not page VRAM out to system
// memory, so the shared/demotion figures stay unknown.
//
// UNVERIFIED on real hardware.
func (n NVML) Sample(ctx context.Context, pids []int) ([]GPU, []ProcessUsage, error) {
	l, err := loadNVML()
	if errors.Is(err, errNoDevice) {
		return nil, nil, nil
	}
	if err != nil {
		if n.Fallback != nil {
			return SampleOf(ctx, n.Fallback, pids)
		}
		return nil, nil, err
	}
	gpus, err := n.GPUs(ctx)
	if err != nil {
		return gpus, make([]ProcessUsage, len(gpus)), err
	}
	set := pidSet(pids)
	usage := make([]ProcessUsage, len(gpus))
	lastSeen := uint64(time.Now().Add(-5 * time.Second).UnixMicro())
	for i := range gpus {
		var h uintptr
		if rc := l.handle(uint32(i), &h); rc != 0 {
			continue
		}
		compute, okC := procMemory(l.computeProcs, h)
		graphics, okG := procMemory(l.graphicsProcs, h)
		if okC || okG {
			var own uint64
			for pid := range set {
				own += max(compute[pid], graphics[pid])
			}
			usage[i].OwnDedicatedMB, usage[i].OwnKnown = int64(own>>20), true
		}
		if l.utilization != nil {
			var u nvmlUtilization
			if rc := l.utilization(h, &u); rc == nvmlSuccess {
				usage[i].UtilPercent, usage[i].UtilKnown = float64(u.GPU), true
				if per, ok := procUtil(l.processUtil, h, lastSeen); ok {
					var ours uint32
					for pid, v := range per {
						if set[pid] {
							ours += v
						}
					}
					usage[i].ForeignUtilPercent = max(float64(u.GPU)-float64(ours), 0)
					usage[i].ForeignUtilKnown = true
				} else if len(set) == 0 {
					usage[i].ForeignUtilPercent, usage[i].ForeignUtilKnown = float64(u.GPU), true
				}
			}
		}
	}
	return gpus, usage, nil
}

func cString(b []byte) string {
	for i, c := range b {
		if c == 0 {
			return string(b[:i])
		}
	}
	return string(b)
}
