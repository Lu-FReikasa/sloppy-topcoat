//go:build unix

package store

import (
	"os"
	"syscall"
)

// ownerOf returns the owner of path (ok=false if it cannot be read).
func ownerOf(path string) (uid, gid int, ok bool) {
	fi, err := os.Stat(path)
	if err != nil {
		return 0, 0, false
	}
	st, isStat := fi.Sys().(*syscall.Stat_t)
	if !isStat {
		return 0, 0, false
	}
	return int(st.Uid), int(st.Gid), true
}

// giveTo makes path belong to uid:gid when running as root; otherwise it is a no-op
// (a normal user cannot give files away, and a file it wrote is its own).
func giveTo(path string, uid, gid int) error {
	if os.Geteuid() != 0 {
		return nil
	}
	return os.Lchown(path, uid, gid)
}
