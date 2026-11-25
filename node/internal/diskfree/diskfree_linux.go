//go:build linux

package diskfree

import "syscall"

func usage(path string) (Usage, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return Usage{}, err
	}
	bs := st.Bsize // int64 on linux/amd64 (the only release target)
	// Bavail is what an ordinary user can still write (not counting the root reserve). Block
	// counts of a real drive are far below 2^63, so the conversions cannot overflow.
	total := int64(st.Blocks) * bs //nolint:gosec // block count of a drive fits int64
	free := int64(st.Bavail) * bs  //nolint:gosec // as above
	return Usage{TotalBytes: total, FreeBytes: free}, nil
}
