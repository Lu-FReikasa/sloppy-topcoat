package winproc

import (
	"errors"
	"testing"
	"unsafe"
)

// list builds what NtQuerySystemInformation(SystemProcessInformation) returns: entries of
// different length, chained by NextEntryOffset, the last one with 0.
func list(entries ...processInfo) []byte {
	const tail = 40 // a process entry is longer than its head (threads follow)
	head := int(unsafe.Sizeof(processInfo{}))
	buf := make([]byte, (head+tail)*len(entries))
	for i, e := range entries {
		e.NextEntryOffset = uint32(head + tail)
		if i == len(entries)-1 {
			e.NextEntryOffset = 0
		}
		*(*processInfo)(unsafe.Pointer(&buf[i*(head+tail)])) = e //nolint:gosec // test buffer is large enough
	}
	return buf
}

func TestSessionFromList(t *testing.T) {
	buf := list(
		processInfo{UniqueProcessID: 0, SessionID: 0},
		processInfo{UniqueProcessID: 628, SessionID: 0}, // the service
		processInfo{UniqueProcessID: 12632, SessionID: 1},
		processInfo{UniqueProcessID: 4711, SessionID: 3},
	)
	for pid, want := range map[uint32]uint32{628: 0, 12632: 1, 4711: 3} {
		if got, err := sessionFromList(buf, pid); err != nil || got != want {
			t.Errorf("pid %d: session %d, %v; want %d", pid, got, err, want)
		}
	}
	if _, err := sessionFromList(buf, 99); !errors.Is(err, ErrNoSuchProcess) {
		t.Errorf("unknown pid: %v", err)
	}
	if _, err := sessionFromList(nil, 1); !errors.Is(err, ErrNoSuchProcess) {
		t.Errorf("empty list: %v", err)
	}
}

func TestTheHeadMatchesTheLayoutOfWindows(t *testing.T) {
	var p processInfo
	if unsafe.Sizeof(uintptr(0)) == 8 {
		if unsafe.Offsetof(p.ImageName) != 56 || unsafe.Offsetof(p.UniqueProcessID) != 80 || unsafe.Offsetof(p.SessionID) != 100 {
			t.Fatalf("offsets %d %d %d, winternl.h says 56 80 100", unsafe.Offsetof(p.ImageName), unsafe.Offsetof(p.UniqueProcessID), unsafe.Offsetof(p.SessionID))
		}
	}
}
