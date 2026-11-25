//go:build unix

package tools

import (
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

// A named pipe inside a released folder is neither listed as a file to read nor opened: opening it
// would block until somebody writes to it.
func TestFilesDoNotOpenANamedPipe(t *testing.T) {
	root, _ := filesTree(t)
	fifo := filepath.Join(root, "pipe.log")
	if err := syscall.Mkfifo(fifo, 0o600); err != nil {
		t.Skip(err)
	}
	done := make(chan error, 1)
	go func() {
		_, err := readFile(t, filesOpts(root), pathArg(fifo))
		done <- err
	}()
	select {
	case err := <-done:
		wantRefused(t, err, "named pipe")
	case <-time.After(3 * time.Second):
		t.Fatal("files.read blocked on a named pipe")
	}
}
