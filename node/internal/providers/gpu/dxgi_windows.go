package gpu

import (
	"context"
	"errors"
	"fmt"
	"math"
	"sync"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// DXGI lists GPUs on Windows (AMD, Intel and NVIDIA) and reads their memory
// usage from the "GPU Adapter Memory" performance counters.
//
// Why DXGI + PDH: DXGI (EnumAdapters1/GetDesc1) gives names, vendor ids,
// dedicated and shared memory totals and the adapter LUID for every vendor
// without a vendor SDK. For *usage*, IDXGIAdapter3::QueryVideoMemoryInfo was
// considered but rejected: it reports the budget and usage of the calling
// process only, not of the whole machine (a game's VRAM would be invisible).
// The PDH counters \GPU Adapter Memory(*)\Dedicated Usage and \Shared Usage
// are system-wide per adapter (the numbers Task Manager shows), keyed by the
// same LUID. They are added with PdhAddEnglishCounterW so that localized
// (e.g. German) Windows installations work.
//
// For VRAM pressure (E1-06) Sample also reads the per-process counters
// \GPU Process Memory(*)\Dedicated Usage / Shared Usage (our model
// processes' own usage and the demotion signal) and \GPU Engine(*)\Utilization
// Percentage (a rate counter: it needs a query that stays open between two
// samples, so the first sample has no utilization).
//
// UNVERIFIED on real hardware.
type DXGI struct {
	mu     sync.Mutex
	engine *pdhQuery // open GPU Engine utilization query (rate counter)
}

var (
	dxgiDLL                 = windows.NewLazySystemDLL("dxgi.dll")
	procCreateDXGIFactory1  = dxgiDLL.NewProc("CreateDXGIFactory1")
	pdhDLL                  = windows.NewLazySystemDLL("pdh.dll")
	procPdhOpenQueryW       = pdhDLL.NewProc("PdhOpenQueryW")
	procPdhAddEnglishCntW   = pdhDLL.NewProc("PdhAddEnglishCounterW")
	procPdhCollectQueryData = pdhDLL.NewProc("PdhCollectQueryData")
	procPdhGetFmtArrayW     = pdhDLL.NewProc("PdhGetFormattedCounterArrayW")
	procPdhCloseQuery       = pdhDLL.NewProc("PdhCloseQuery")
	iidIDXGIFactory1        = windows.GUID{Data1: 0x770aae78, Data2: 0xf26f, Data3: 0x4dba, Data4: [8]byte{0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87}}
)

const (
	dxgiErrorNotFound   = 0x887A0002
	dxgiAdapterSoftware = 2
	pdhFmtDouble        = 0x00000200
	pdhFmtLarge         = 0x00000400
	pdhMoreData         = 0x800007D2
)

type dxgiAdapterDesc1 struct {
	Description           [128]uint16
	VendorID              uint32
	DeviceID              uint32
	SubSysID              uint32
	Revision              uint32
	DedicatedVideoMemory  uintptr
	DedicatedSystemMemory uintptr
	SharedSystemMemory    uintptr
	LUIDLow               uint32
	LUIDHigh              int32
	Flags                 uint32
}

// comObject is the memory layout of a COM interface pointer's target.
type comObject struct {
	vtbl *[32]uintptr
}

// comCall calls method index idx of a COM object's vtable.
func comCall(obj *comObject, idx int, args ...uintptr) uintptr {
	r, _, _ := syscall.SyscallN(obj.vtbl[idx], append([]uintptr{uintptr(unsafe.Pointer(obj))}, args...)...)
	return r
}

const (
	vtRelease       = 2
	vtEnumAdapters1 = 12 // IDXGIFactory1
	vtGetDesc1      = 10 // IDXGIAdapter1
)

func enumAdapters() ([]adapter, error) {
	if err := procCreateDXGIFactory1.Find(); err != nil {
		return nil, err
	}
	var factory *comObject
	if hr, _, _ := procCreateDXGIFactory1.Call(uintptr(unsafe.Pointer(&iidIDXGIFactory1)), uintptr(unsafe.Pointer(&factory))); hr != 0 {
		return nil, fmt.Errorf("CreateDXGIFactory1: 0x%08x", uint32(hr))
	}
	defer comCall(factory, vtRelease)
	var out []adapter
	for i := uintptr(0); i < 64; i++ {
		var ad *comObject
		hr := comCall(factory, vtEnumAdapters1, i, uintptr(unsafe.Pointer(&ad)))
		if uint32(hr) == dxgiErrorNotFound {
			break
		}
		if hr != 0 {
			return out, fmt.Errorf("EnumAdapters1(%d): 0x%08x", i, uint32(hr))
		}
		var d dxgiAdapterDesc1
		hr = comCall(ad, vtGetDesc1, uintptr(unsafe.Pointer(&d)))
		comCall(ad, vtRelease)
		if hr != 0 {
			continue
		}
		out = append(out, adapter{
			Name: windows.UTF16ToString(d.Description[:]), VendorID: d.VendorID,
			DedicatedMB: int64(d.DedicatedVideoMemory >> 20), SharedMB: int64(d.SharedSystemMemory >> 20),
			LUID: LUID{High: d.LUIDHigh, Low: d.LUIDLow}, Software: d.Flags&dxgiAdapterSoftware != 0,
		})
	}
	return out, nil
}

type pdhCounterValueItem struct {
	Name    *uint16
	CStatus uint32
	_       uint32
	Value   int64
}

// pdhQuery is an open PDH query with its counters.
type pdhQuery struct {
	q        uintptr
	counters []uintptr
}

// openQuery opens a query with the given (English, wildcard) counter paths.
func openQuery(paths ...string) (*pdhQuery, error) {
	if err := procPdhOpenQueryW.Find(); err != nil {
		return nil, err
	}
	pq := &pdhQuery{}
	if r, _, _ := procPdhOpenQueryW.Call(0, 0, uintptr(unsafe.Pointer(&pq.q))); r != 0 {
		return nil, fmt.Errorf("PdhOpenQueryW: 0x%08x", uint32(r))
	}
	for _, path := range paths {
		p, _ := windows.UTF16PtrFromString(path)
		var c uintptr
		if r, _, _ := procPdhAddEnglishCntW.Call(pq.q, uintptr(unsafe.Pointer(p)), 0, uintptr(unsafe.Pointer(&c))); r != 0 {
			pq.close()
			return nil, fmt.Errorf("PdhAddEnglishCounterW(%s): 0x%08x", path, uint32(r))
		}
		pq.counters = append(pq.counters, c)
	}
	return pq, nil
}

func (pq *pdhQuery) collect() error {
	if r, _, _ := procPdhCollectQueryData.Call(pq.q); r != 0 {
		return fmt.Errorf("PdhCollectQueryData: 0x%08x", uint32(r))
	}
	return nil
}

func (pq *pdhQuery) close() {
	_, _, _ = procPdhCloseQuery.Call(pq.q) // best effort
}

// read returns the values of counter i per instance (summed over duplicates).
// double selects PDH_FMT_DOUBLE (rates) instead of PDH_FMT_LARGE (bytes).
func (pq *pdhQuery) read(i int, double bool) (map[string]float64, error) {
	format := uintptr(pdhFmtLarge)
	if double {
		format = pdhFmtDouble
	}
	c := pq.counters[i]
	var size, count uint32
	r, _, _ := procPdhGetFmtArrayW.Call(c, format, uintptr(unsafe.Pointer(&size)), uintptr(unsafe.Pointer(&count)), 0)
	if uint32(r) != pdhMoreData {
		return nil, fmt.Errorf("PdhGetFormattedCounterArrayW: 0x%08x", uint32(r))
	}
	buf := make([]byte, size)
	r, _, _ = procPdhGetFmtArrayW.Call(c, format, uintptr(unsafe.Pointer(&size)), uintptr(unsafe.Pointer(&count)), uintptr(unsafe.Pointer(&buf[0])))
	if r != 0 {
		return nil, fmt.Errorf("PdhGetFormattedCounterArrayW: 0x%08x", uint32(r))
	}
	items := unsafe.Slice((*pdhCounterValueItem)(unsafe.Pointer(&buf[0])), count)
	out := map[string]float64{}
	for _, it := range items {
		if it.CStatus != 0 && it.CStatus != 1 { // PDH_CSTATUS_VALID_DATA / NEW_DATA
			continue
		}
		v := float64(it.Value)
		if double {
			v = math.Float64frombits(uint64(it.Value))
		}
		out[windows.UTF16PtrToString(it.Name)] += v
	}
	return out, nil
}

// counterPair collects two counters of one object once and reads both (bytes).
func counterPair(a, b string) (map[string]float64, map[string]float64, error) {
	pq, err := openQuery(a, b)
	if err != nil {
		return nil, nil, err
	}
	defer pq.close()
	if err := pq.collect(); err != nil {
		return nil, nil, err
	}
	va, err := pq.read(0, false)
	if err != nil {
		return nil, nil, err
	}
	vb, err := pq.read(1, false)
	return va, vb, err
}

// adapterMemoryCounters returns Dedicated Usage and Shared Usage per instance (bytes).
func adapterMemoryCounters() (dedicated, shared map[string]int64, err error) {
	d, s, err := counterPair(`\GPU Adapter Memory(*)\Dedicated Usage`, `\GPU Adapter Memory(*)\Shared Usage`)
	if err != nil {
		return nil, nil, err
	}
	return toInt64(d), toInt64(s), nil
}

func toInt64(m map[string]float64) map[string]int64 {
	out := make(map[string]int64, len(m))
	for k, v := range m {
		out[k] = int64(v)
	}
	return out
}

// engineUtil samples the GPU Engine utilization rates since the previous call.
func (d *DXGI) engineUtil() (map[string]float64, error) {
	if d.engine == nil {
		pq, err := openQuery(`\GPU Engine(*)\Utilization Percentage`)
		if err != nil {
			return nil, err
		}
		d.engine = pq
		if err := pq.collect(); err != nil {
			return nil, err
		}
		return nil, errors.New("first utilization sample")
	}
	if err := d.engine.collect(); err != nil {
		return nil, err
	}
	return d.engine.read(0, true)
}

// GPUs implements GPUProvider.
func (d *DXGI) GPUs(context.Context) ([]GPU, error) {
	gpus, _, err := d.gpus()
	return gpus, err
}

func (d *DXGI) gpus() ([]GPU, []LUID, error) {
	adapters, err := enumAdapters()
	if err != nil {
		return nil, nil, fmt.Errorf("dxgi: %w", err)
	}
	ded, sh, perr := adapterMemoryCounters()
	gpus, luids := buildGPUs(adapters, sumByLUID(ded), sumByLUID(sh))
	if perr != nil {
		// Totals are still useful without usage numbers.
		return gpus, luids, errors.Join(errors.New("gpu memory counters unavailable"), perr)
	}
	return gpus, luids, nil
}

// Sample implements Sampler: GPUs plus the usage of our model processes and
// other programs' engine utilization. Missing per-process counters leave the
// usage unknown instead of failing.
func (d *DXGI) Sample(_ context.Context, pids []int) ([]GPU, []ProcessUsage, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	gpus, luids, err := d.gpus()
	usage := make([]ProcessUsage, len(gpus))
	if err != nil {
		return gpus, usage, err
	}
	set := pidSet(pids)
	if pd, ps, perr := counterPair(`\GPU Process Memory(*)\Dedicated Usage`, `\GPU Process Memory(*)\Shared Usage`); perr == nil {
		ownD, ownS := ownMemoryByLUID(pd, set), ownMemoryByLUID(ps, set)
		foreignS := foreignMaxByLUID(ps, set)
		for i, l := range luids {
			usage[i].OwnDedicatedMB, usage[i].OwnKnown = ownD[l]>>20, true
			usage[i].OwnSharedMB, usage[i].SharedKnown = ownS[l]>>20, true
			usage[i].ForeignSharedMaxMB, usage[i].ForeignSharedKnown = foreignS[l]>>20, true
		}
	}
	if util, uerr := d.engineUtil(); uerr == nil {
		total, foreign := engineUtilByLUID(util, set)
		for i, l := range luids {
			usage[i].UtilPercent, usage[i].UtilKnown = total[l], true
			usage[i].ForeignUtilPercent, usage[i].ForeignUtilKnown = foreign[l], true
		}
	}
	return gpus, usage, nil
}

// NewSystem returns the GPU provider for this OS (Windows: DXGI + PDH for all vendors).
func NewSystem() GPUProvider { return Multi{&DXGI{}} }
