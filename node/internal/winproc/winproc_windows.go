package winproc

import (
	"errors"
	"fmt"
	"unsafe"

	"golang.org/x/sys/windows"
)

// systemProcessIDInformation is SystemProcessIdInformation (class 88): the image path of any
// process by pid.
const systemProcessIDInformation = 88

type processIDInformation struct {
	ProcessID uintptr
	ImageName windows.NTUnicodeString
}

// UNVERIFIED on real hardware for a service account asking about a user's process (the Windows CI
// job checks the own process; the image path part is the query of D17).

// NTImagePath returns the NT path (\Device\HarddiskVolumeN\...) of pid, or "" if Windows does not
// tell. buf is scratch space (32767 characters are enough for every path).
func NTImagePath(pid uint32, buf []uint16) string {
	info := processIDInformation{ProcessID: uintptr(pid)}
	info.ImageName.MaximumLength = uint16(2 * len(buf)) //nolint:gosec // len(buf) is 32767
	info.ImageName.Buffer = &buf[0]
	if err := windows.NtQuerySystemInformation(systemProcessIDInformation, unsafe.Pointer(&info), uint32(unsafe.Sizeof(info)), nil); err != nil {
		return ""
	}
	return windows.UTF16ToString(buf[:info.ImageName.Length/2])
}

// DosDrives maps drive letters to their NT device ("C:" -> \Device\HarddiskVolume3).
func DosDrives() map[string]string {
	out := map[string]string{}
	buf := make([]uint16, 1024)
	for c := 'A'; c <= 'Z'; c++ {
		letter := string(c) + ":"
		name, err := windows.UTF16PtrFromString(letter)
		if err != nil {
			continue
		}
		n, err := windows.QueryDosDevice(name, &buf[0], uint32(len(buf)))
		if err != nil || n == 0 {
			continue
		}
		out[letter] = windows.UTF16ToString(buf[:n])
	}
	return out
}

// ImagePath is the Win32 path of the program file of pid (C:\...).
func ImagePath(pid uint32) (string, error) {
	nt := NTImagePath(pid, make([]uint16, 32767))
	if nt == "" {
		return "", fmt.Errorf("Windows gives no program file for pid %d (NtQuerySystemInformation)", pid) //nolint:staticcheck // Windows is a proper noun
	}
	if p := NTToDosPath(nt, DosDrives()); p != "" {
		return p, nil
	}
	return "", fmt.Errorf("the program file %q of pid %d is on a device that has no drive letter", nt, pid)
}

// SessionOf is the session id of pid, from the process list (NtQuerySystemInformation,
// SystemProcessInformation), which needs no access to the process.
func SessionOf(pid uint32) (uint32, error) {
	size := 1 << 20
	for range 8 {
		buf := make([]byte, size)
		var need uint32
		err := windows.NtQuerySystemInformation(windows.SystemProcessInformation, unsafe.Pointer(&buf[0]), uint32(len(buf)), &need) //nolint:gosec // len(buf) is at most a few tens of MB
		switch {
		case err == nil:
			return sessionFromList(buf, pid)
		case errors.Is(err, windows.STATUS_INFO_LENGTH_MISMATCH):
			size = max(size*2, int(need)+(1<<16))
		default:
			return 0, fmt.Errorf("process list: %w", err)
		}
	}
	return 0, errors.New("process list: it keeps growing")
}
