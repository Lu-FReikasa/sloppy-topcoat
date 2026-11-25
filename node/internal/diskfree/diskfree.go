// Package diskfree reports free and total space of the drive a path lives on.
package diskfree

import (
	"os"
	"path/filepath"
)

// Usage is the space of one drive in bytes.
type Usage struct {
	TotalBytes int64
	FreeBytes  int64
}

// Func measures the drive of path (injectable for tests).
type Func func(path string) (Usage, error)

// Of measures the drive that holds path; a path that does not exist yet is measured
// through its nearest existing parent (the model directory is created on the first
// download).
func Of(path string) (Usage, error) {
	p := filepath.Clean(path)
	for {
		if _, err := os.Stat(p); err == nil {
			break
		}
		parent := filepath.Dir(p)
		if parent == p {
			break
		}
		p = parent
	}
	return usage(p)
}
