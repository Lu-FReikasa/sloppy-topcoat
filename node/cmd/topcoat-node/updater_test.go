package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"topcoat/node/internal/update"
	"topcoat/node/internal/version"
)

// confirmingHost plays the OS: starting the node "runs" the new version, which confirms itself.
type confirmingHost struct {
	layout   update.Layout
	version  string
	replaced []byte
}

func (h *confirmingHost) StopNode(context.Context) error { return nil }
func (h *confirmingHost) StartNode(context.Context) error {
	return h.layout.WriteConfirmed(h.version, time.Now())
}
func (h *confirmingHost) ReplaceProgram(_ context.Context, data []byte) error {
	h.replaced = data
	return nil
}
func (h *confirmingHost) RestoreProgram(context.Context) error { return nil }

func withUpdateKeys(t *testing.T, keys string) {
	t.Helper()
	old := version.UpdateKeys
	version.UpdateKeys = keys
	t.Cleanup(func() { version.UpdateKeys = old })
}

func TestUpdaterAppliesAStagedBuildAndEndsWithANonZeroCode(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	withUpdateKeys(t, base64.StdEncoding.EncodeToString(pub))
	dir := t.TempDir()
	layout := update.NewLayout(dir)
	bin := []byte("new program")
	m := update.Manifest{Format: 1, Product: update.Product, Channel: "stable", Version: "9.9.9", OS: runtime.GOOS, Arch: runtime.GOARCH,
		Size: int64(len(bin)), SHA256: update.SHA256Hex(bin), Created: "2026-10-07T20:00:00Z"}
	mb, _ := json.Marshal(m)
	if err := os.MkdirAll(layout.Staging(), 0o700); err != nil {
		t.Fatal(err)
	}
	for name, data := range map[string][]byte{update.ManifestFile: mb, update.SigFile: []byte(update.Sign(priv, mb)), update.BinaryFile: bin} {
		if err := os.WriteFile(filepath.Join(layout.Staging(), name), data, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := layout.MarkReady("9.9.9", time.Now()); err != nil {
		t.Fatal(err)
	}
	host := &confirmingHost{layout: layout, version: "9.9.9"}
	old := newUpdateHost
	newUpdateHost = func() update.Host { return host }
	t.Cleanup(func() { newUpdateHost = old })

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	code, _, stderr := runCmd(ctx, "updater", "--data-dir", dir)
	if code != exitUpdated {
		t.Fatalf("exit %d, want %d\n%s", code, exitUpdated, stderr)
	}
	if string(host.replaced) != "new program" {
		t.Fatalf("the program file got %q", host.replaced)
	}
}

func TestUpdaterWithoutKeysStaysIdle(t *testing.T) {
	withUpdateKeys(t, "")
	dir := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan int, 1)
	go func() { code, _, _ := runCmd(ctx, "updater", "--data-dir", dir); done <- code }()
	select {
	case code := <-done:
		t.Fatalf("the updater ended with %d although it has nothing to do", code)
	case <-time.After(200 * time.Millisecond):
	}
	cancel()
	if code := <-done; code != 0 {
		t.Fatalf("exit %d", code)
	}
	if _, err := os.Stat(filepath.Join(dir, "update", "status.json")); err == nil {
		t.Fatal("an updater without keys must not report itself alive")
	}
}

func TestUpdaterRefusesABrokenKey(t *testing.T) {
	withUpdateKeys(t, "not-a-key")
	if code, _, _ := runCmd(context.Background(), "updater", "--data-dir", t.TempDir()); code != 1 {
		t.Fatalf("exit %d, want 1", code)
	}
}
