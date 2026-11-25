// Package winproc answers two questions about any Windows process by its pid without opening it,
// so the virtual service account of the node may ask them about another user's process too (D17):
// in which session does it run, and which program file is it.
//
// Both come from NtQuerySystemInformation, which needs no process handle. OpenProcess,
// ProcessIdToSessionId and QueryFullProcessImageName are refused for such processes ("Zugriff
// verweigert" in the hardware test of the input helper). The parsing is plain Go and tested on
// every OS; the calls sit in winproc_windows.go.
package winproc

import (
	"errors"
	"unsafe"
)

// processInfo is the head of SYSTEM_PROCESS_INFORMATION (winternl.h) up to SessionId. Go lays the
// fields out like the C compiler does, so the offsets are the same on 32 and 64 bit.
type processInfo struct {
	NextEntryOffset uint32
	NumberOfThreads uint32
	Reserved1       [48]byte
	ImageName       [2]uintptr // UNICODE_STRING: 2 x uint16 + pointer, not read here
	BasePriority    int32
	UniqueProcessID uintptr
	Reserved2       uintptr
	HandleCount     uint32
	SessionID       uint32
}

// ErrNoSuchProcess says the pid is not in the process list.
var ErrNoSuchProcess = errors.New("no such process")

// sessionFromList finds pid in a buffer filled by NtQuerySystemInformation(SystemProcessInformation)
// and returns its session id.
func sessionFromList(buf []byte, pid uint32) (uint32, error) {
	head := int(unsafe.Sizeof(processInfo{}))
	off := 0
	for off+head <= len(buf) {
		e := (*processInfo)(unsafe.Pointer(&buf[off])) //nolint:gosec // bounds checked above; the buffer is the kernel's answer
		if uint32(e.UniqueProcessID) == pid {          //nolint:gosec // a pid fits 32 bits
			return e.SessionID, nil
		}
		if e.NextEntryOffset == 0 {
			break
		}
		off += int(e.NextEntryOffset)
	}
	return 0, ErrNoSuchProcess
}
