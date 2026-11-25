//go:build !windows

package tools

import (
	"errors"
	"io/fs"
	"os"
	"syscall"
)

// checkSecretFile: a regular file (not a link), mode 600 at most, owned by this process's user
// or by root.
func checkSecretFile(path string, st fs.FileInfo) error {
	if st.Mode()&os.ModeSymlink != 0 || !st.Mode().IsRegular() {
		return errors.New("connectors file: must be a regular file, not a link")
	}
	if st.Mode().Perm()&0o077 != 0 {
		return errors.New("connectors file: readable by others; make it private (chmod 600, owned by the node's user)")
	}
	if sys, ok := st.Sys().(*syscall.Stat_t); ok {
		if uid := int(sys.Uid); uid != os.Geteuid() && uid != 0 {
			return errors.New("connectors file: belongs to another user; it must be owned by the node's user (or root)")
		}
	}
	return nil
}
