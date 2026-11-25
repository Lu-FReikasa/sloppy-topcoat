// Package logfile is a small size-rotating log writer for the Windows
// service, which has no console (on Linux journald captures stderr).
package logfile

import (
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// Writer appends to Path and rotates it to Path.1 … Path.<Keep> when it grows
// beyond MaxBytes.
type Writer struct {
	Path     string
	MaxBytes int64
	Keep     int

	mu   sync.Mutex
	f    *os.File
	size int64
}

// Open opens (or creates) the log file.
func Open(path string, maxBytes int64, keep int) (*Writer, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		return nil, err
	}
	w := &Writer{Path: path, MaxBytes: maxBytes, Keep: keep}
	return w, w.open()
}

func (w *Writer) open() error {
	f, err := os.OpenFile(w.Path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	st, err := f.Stat()
	if err != nil {
		_ = f.Close()
		return err
	}
	w.f, w.size = f, st.Size()
	return nil
}

// Write implements io.Writer.
func (w *Writer) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.f == nil {
		return 0, os.ErrClosed
	}
	if w.MaxBytes > 0 && w.size+int64(len(p)) > w.MaxBytes && w.size > 0 {
		if err := w.rotate(); err != nil {
			return 0, err
		}
	}
	n, err := w.f.Write(p)
	w.size += int64(n)
	return n, err
}

func (w *Writer) rotate() error {
	if err := w.f.Close(); err != nil {
		return err
	}
	for i := w.Keep; i >= 1; i-- {
		src := w.Path
		if i > 1 {
			src = fmt.Sprintf("%s.%d", w.Path, i-1)
		}
		_ = os.Rename(src, fmt.Sprintf("%s.%d", w.Path, i))
	}
	if w.Keep < 1 {
		_ = os.Remove(w.Path)
	}
	return w.open()
}

// Close closes the file.
func (w *Writer) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.f == nil {
		return nil
	}
	err := w.f.Close()
	w.f = nil
	return err
}
