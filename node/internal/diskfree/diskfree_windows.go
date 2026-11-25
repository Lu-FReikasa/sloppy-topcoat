//go:build windows

package diskfree

import "golang.org/x/sys/windows"

// UNVERIFIED on real hardware: GetDiskFreeSpaceExW for the drive of the path.
func usage(path string) (Usage, error) {
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return Usage{}, err
	}
	var avail, total, free uint64
	if err := windows.GetDiskFreeSpaceEx(p, &avail, &total, &free); err != nil {
		return Usage{}, err
	}
	return Usage{TotalBytes: int64(total), FreeBytes: int64(avail)}, nil
}
