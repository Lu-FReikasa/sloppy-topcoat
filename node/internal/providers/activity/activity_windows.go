package activity

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"sync"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"

	"topcoat/node/internal/inputhelper"
)

// UNVERIFIED on real hardware: everything in this file. The virtual service account
// NT SERVICE\topcoat-node (D17) may not query the console session with
// WTSQuerySessionInformation ("Zugriff verweigert", seen on the real laptop), so keyboard and mouse
// input comes from the input helper in the user's session first (package inputhelper: scheduled
// task at logon, named pipe); the WTS query stays as the fallback for machines where it works.
// If neither answers, input stays "not known" (and says why once) and the node keeps working on
// the CPU-load and battery signals.

var (
	wtsapi32                = windows.NewLazySystemDLL("wtsapi32.dll")
	procWTSQuerySession     = wtsapi32.NewProc("WTSQuerySessionInformationW")
	kernel32                = windows.NewLazySystemDLL("kernel32.dll")
	procGetSystemTimes      = kernel32.NewProc("GetSystemTimes")
	procGetSystemPowerState = kernel32.NewProc("GetSystemPowerStatus")
)

const (
	wtsSessionInfo    = 24 // WTS_INFO_CLASS WTSSessionInfo
	noConsoleSession  = 0xFFFFFFFF
	acLineOnline      = 1
	acLineOffline     = 0
	batteryFlagNoBatt = 128
)

type system struct {
	mu        sync.Mutex
	prevTotal float64
	prevBusy  float64
	prevOwn   map[int]float64
	// prevGuest is the CPU time of the VM host processes (vmmem …) by pid at the previous sample.
	prevGuest map[int]float64
	havePrev  bool
	pdh       *pdhCPU // opened with the first sample
}

// NewSystem returns the Windows provider: the console session's last input time from
// WTSQuerySessionInformation (works from session 0), CPU from GetSystemTimes and
// GetProcessTimes, battery from GetSystemPowerStatus.
func NewSystem() Provider { return &system{} }

func (s *system) Input(context.Context) (time.Duration, bool) {
	idle, known, _ := consoleInputIdle()
	return idle, known
}

func (s *system) Sample(_ context.Context, ownPIDs []int) Sample {
	var out Sample
	out.InputIdle, out.InputKnown, out.InputErr = consoleInputIdle()
	out.ForeignCPU, out.CPUKnown, out.CPUNote = s.cpu(ownPIDs)
	out.OnBattery, out.BatteryKnown = powerState()
	return out
}

func consoleInputIdle() (idle time.Duration, known bool, why string) {
	sid := windows.WTSGetActiveConsoleSessionId()
	if sid == noConsoleSession {
		return 0, false, "" // nobody is logged on at the console
	}
	d, err := inputhelper.IdleFromHelper(sid)
	if err == nil {
		return d, true, ""
	}
	idle, known, wtsWhy := wtsInputIdle(sid)
	if known || wtsWhy == "" {
		return idle, known, ""
	}
	if errors.Is(err, inputhelper.ErrNoHelper) {
		return 0, false, "no input helper answers in the console session (it is started at logon by the scheduled task " +
			"topcoat-node-input that `topcoat-node install` creates: log on again or run the task) and " + wtsWhy
	}
	return 0, false, fmt.Sprintf("the input helper failed (%v) and %s", err, wtsWhy)
}

// wtsInputIdle asks Windows for the console session's last input. why is set when the query
// failed (a service account may lack the right to ask).
func wtsInputIdle(sid uint32) (idle time.Duration, known bool, why string) {
	var buf *byte
	var n uint32
	r, _, err := procWTSQuerySession.Call(0 /* WTS_CURRENT_SERVER_HANDLE */, uintptr(sid), wtsSessionInfo,
		uintptr(unsafe.Pointer(&buf)), uintptr(unsafe.Pointer(&n)))
	if r == 0 {
		return 0, false, fmt.Sprintf("WTSQuerySessionInformation for the console session failed (a service account may lack the right to ask): %v", err)
	}
	defer windows.WTSFreeMemory(uintptr(unsafe.Pointer(buf))) //nolint:gosec // the pointer WTS handed out
	b := unsafe.Slice(buf, n)                                 // owned by WTS until WTSFreeMemory
	if st := wtsState(b); st != wtsStateActive {
		// disconnected / locked away from the console: nobody is typing
		return 24 * time.Hour, true, ""
	}
	d, ok := idleFromWTSInfo(b)
	if !ok {
		return 0, false, ""
	}
	return d, true, ""
}

func systemTimes() (idle, kernel, user uint64, ok bool) {
	var i, k, u windows.Filetime
	r, _, _ := procGetSystemTimes.Call(uintptr(unsafe.Pointer(&i)), uintptr(unsafe.Pointer(&k)), uintptr(unsafe.Pointer(&u)))
	if r == 0 {
		return 0, 0, 0, false
	}
	ft := func(f windows.Filetime) uint64 { return uint64(f.HighDateTime)<<32 | uint64(f.LowDateTime) }
	return ft(i), ft(k), ft(u), true
}

func processTimes(pid int) (float64, bool) {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return 0, false
	}
	defer windows.CloseHandle(h) //nolint:errcheck // best effort
	var c, e, k, u windows.Filetime
	if err := windows.GetProcessTimes(h, &c, &e, &k, &u); err != nil {
		return 0, false
	}
	ft := func(f windows.Filetime) float64 { return float64(uint64(f.HighDateTime)<<32 | uint64(f.LowDateTime)) }
	return ft(k) + ft(u), true
}

func (s *system) cpu(ownPIDs []int) (float64, bool, string) {
	idle, kernel, user, ok := systemTimes()
	if !ok {
		return 0, false, ""
	}
	total := float64(kernel + user) // the kernel time already contains the idle time
	busy := total - float64(idle)
	ticks := make(map[int]float64, len(ownPIDs))
	for _, pid := range ownPIDs {
		if t, ok := processTimes(pid); ok {
			ticks[pid] = t
		}
	}
	guest, unreadable := guestTimes()
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.pdh == nil {
		s.pdh = openPDHCPU()
	}
	utility, hyperV := s.pdh.read() // collects: the figures cover the time since the previous sample
	note := s.pdh.note()
	if unreadable > 0 {
		note += "; vmmem: cannot open " + strconv.Itoa(unreadable) + " process(es)"
	}
	prevTotal, prevBusy, prevOwn, prevGuest, had := s.prevTotal, s.prevBusy, s.prevOwn, s.prevGuest, s.havePrev
	s.prevTotal, s.prevBusy, s.prevOwn, s.prevGuest, s.havePrev = total, busy, ticks, guest, true
	if !had || total-prevTotal <= 0 {
		return 0, false, note
	}
	var own, gst float64
	for pid, t := range ticks {
		own += max(t-prevOwn[pid], 0)
	}
	for pid, t := range guest {
		gst += max(t-prevGuest[pid], 0)
	}
	span := total - prevTotal
	return CPULoad{
		Time:    (busy - prevBusy) / span * 100,
		Own:     own / span * 100,
		Utility: utility,
		HyperV:  hyperV,
		Guest:   gst / span * 100,
	}.Foreign(), true, note
}

type systemPowerStatus struct {
	ACLineStatus        byte
	BatteryFlag         byte
	BatteryLifePercent  byte
	SystemStatusFlag    byte
	BatteryLifeTime     uint32
	BatteryFullLifeTime uint32
}

func powerState() (onBattery, known bool) {
	var st systemPowerStatus
	r, _, _ := procGetSystemPowerState.Call(uintptr(unsafe.Pointer(&st)))
	if r == 0 || st.BatteryFlag == batteryFlagNoBatt || st.ACLineStatus > acLineOnline {
		return false, false // no battery (a desktop) or unknown
	}
	return st.ACLineStatus == acLineOffline, true
}
