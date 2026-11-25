package service

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestFileHostReplaceAndRestore(t *testing.T) {
	dir := t.TempDir()
	prog := filepath.Join(dir, "topcoat-node")
	if err := os.WriteFile(prog, []byte("old"), 0o755); err != nil { //nolint:gosec // test
		t.Fatal(err)
	}
	h := &fileHost{program: prog}
	ctx := context.Background()
	read := func() string { b, _ := os.ReadFile(prog); return string(b) }

	if err := h.ReplaceProgram(ctx, []byte("new")); err != nil || read() != "new" {
		t.Fatalf("replace: %v, file %q", err, read())
	}
	if err := h.RestoreProgram(ctx); err != nil || read() != "old" {
		t.Fatalf("restore: %v, file %q", err, read())
	}
	// Restoring twice (nothing kept any more) leaves the file alone.
	if err := h.RestoreProgram(ctx); err != nil || read() != "old" {
		t.Fatalf("second restore: %v, file %q", err, read())
	}
}

func TestFileHostDropsALeftoverFromAnOlderUpdate(t *testing.T) {
	dir := t.TempDir()
	prog := filepath.Join(dir, "topcoat-node")
	_ = os.WriteFile(prog, []byte("current"), 0o755)         //nolint:gosec // test
	_ = os.WriteFile(prog+".prev", []byte("ancient"), 0o755) //nolint:gosec // test
	h := &fileHost{program: prog}
	if err := h.ReplaceProgram(context.Background(), []byte("next")); err != nil {
		t.Fatal(err)
	}
	if err := h.RestoreProgram(context.Background()); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(prog); string(b) != "current" {
		t.Fatalf("restored %q, want the file that was replaced, not the leftover", b)
	}
}

func TestFileHostWithoutAProgramFile(t *testing.T) {
	prog := filepath.Join(t.TempDir(), "topcoat-node")
	h := &fileHost{program: prog}
	if err := h.ReplaceProgram(context.Background(), []byte("new")); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(prog); string(b) != "new" {
		t.Fatalf("file %q", b)
	}
}
