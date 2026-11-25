package activity

import (
	"encoding/binary"
	"time"
)

// Layout of WTSINFOW (wtsapi32.h), the answer of WTSQuerySessionInformation(WTSSessionInfo):
// State, SessionId and six DWORD counters (32 bytes), WinStationName[32], Domain[17],
// UserName[21] as UTF-16 (64 + 34 + 42 bytes), padding to 8 bytes, then five LARGE_INTEGER
// times: ConnectTime, DisconnectTime, LastInputTime, LogonTime, CurrentTime, in 100 ns units.
const (
	wtsInfoSize           = 216
	wtsInfoStateOffset    = 0
	wtsInfoLastInputAt    = 192
	wtsInfoCurrentTimeAt  = 208
	wtsStateActive        = 0
	filetimeTicksPerNanos = 100
)

// idleFromWTSInfo turns a WTSINFOW buffer into the time since the last input of that session.
// A session without any input yet (LastInputTime 0) or a buffer that is too short is unknown.
func idleFromWTSInfo(b []byte) (time.Duration, bool) {
	if len(b) < wtsInfoSize {
		return 0, false
	}
	last := int64(binary.LittleEndian.Uint64(b[wtsInfoLastInputAt:]))  //nolint:gosec // a FILETIME stays far below 2^63
	cur := int64(binary.LittleEndian.Uint64(b[wtsInfoCurrentTimeAt:])) //nolint:gosec // as above
	if last <= 0 || cur <= 0 {
		return 0, false
	}
	if cur < last {
		return 0, true // clock skew between the two stamps: the input is "now"
	}
	return time.Duration((cur - last) * filetimeTicksPerNanos), true
}

// wtsState is the connection state of the session (0 = active on the console).
func wtsState(b []byte) uint32 {
	if len(b) < 4 {
		return 0xFFFFFFFF
	}
	return binary.LittleEndian.Uint32(b[wtsInfoStateOffset:])
}
