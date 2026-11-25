package tools

import (
	"errors"
	"io/fs"
	"os"
)

// checkSecretFile on Windows: a regular file, not a link. UNVERIFIED on real hardware: the
// permission bits mean nothing there, so the ACL of the file (only the service account and the
// administrators) is for the maintainer to set; INSTALL-NODE says how.
func checkSecretFile(_ string, st fs.FileInfo) error {
	if st.Mode()&os.ModeSymlink != 0 || !st.Mode().IsRegular() {
		return errors.New("connectors file: must be a regular file, not a link")
	}
	return nil
}
