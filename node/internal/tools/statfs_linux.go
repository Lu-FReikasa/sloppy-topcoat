//go:build linux

package tools

import "syscall"

func statFS(path string) (FSStat, error) {
	var s syscall.Statfs_t
	if err := syscall.Statfs(path, &s); err != nil {
		return FSStat{}, err
	}
	bs := uint64(s.Bsize) //nolint:gosec // a block size is positive
	return FSStat{
		TotalBytes: s.Blocks * bs, FreeBytes: s.Bfree * bs, AvailBytes: s.Bavail * bs,
		Inodes: s.Files, InodesFree: s.Ffree,
	}, nil
}
