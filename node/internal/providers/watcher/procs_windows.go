package watcher

import (
	"path/filepath"
	"unsafe"

	"golang.org/x/sys/windows"

	"topcoat/node/internal/winproc"
)

// listProcesses enumerates processes with a Toolhelp snapshot and resolves the
// full image path with NtQuerySystemInformation(SystemProcessIdInformation).
// Unlike OpenProcess + QueryFullProcessImageNameW this needs no access to the
// process, so the virtual service account NT SERVICE\topcoat-node sees the
// paths of the logged-in user's games too (D17, test 1 A7). OpenProcess stays
// as a fallback.
//
// UNVERIFIED on real hardware (the Windows CI job checks the own process).
func listProcesses() ([]Proc, error) {
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return nil, err
	}
	defer windows.CloseHandle(snap) //nolint:errcheck // best effort
	drives := winproc.DosDrives()
	buf := make([]uint16, 32767)
	var pe windows.ProcessEntry32
	pe.Size = uint32(unsafe.Sizeof(pe))
	var out []Proc
	for err = windows.Process32First(snap, &pe); err == nil; err = windows.Process32Next(snap, &pe) {
		p := Proc{Name: windows.UTF16ToString(pe.ExeFile[:])}
		if path := imagePath(pe.ProcessID, buf, drives); path != "" {
			p.Path = path
			p.Name = filepath.Base(path)
		}
		out = append(out, p)
	}
	return out, nil
}

func imagePath(pid uint32, buf []uint16, drives map[string]string) string {
	if pid == 0 || pid == 4 {
		return "" // System Idle Process / System
	}
	if nt := winproc.NTImagePath(pid, buf); nt != "" {
		if p := winproc.NTToDosPath(nt, drives); p != "" {
			return p
		}
	}
	return handleImagePath(pid)
}

// handleImagePath is the old way; it needs PROCESS_QUERY_LIMITED_INFORMATION,
// which a virtual service account does not get for other users' processes.
func handleImagePath(pid uint32) string {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil {
		return ""
	}
	defer windows.CloseHandle(h) //nolint:errcheck // best effort
	buf := make([]uint16, windows.MAX_LONG_PATH)
	n := uint32(len(buf))
	if err := windows.QueryFullProcessImageName(h, 0, &buf[0], &n); err != nil {
		return ""
	}
	return windows.UTF16ToString(buf[:n])
}
