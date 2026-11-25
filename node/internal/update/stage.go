package update

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"
)

// Source is where the node gets a build from: the hub, over the node's authenticated TLS
// connection. A fake serves the tests.
type Source interface {
	// Manifest returns the signed manifest and the signature line of version; "" asks for the
	// newest build of the node's channel and platform.
	Manifest(ctx context.Context, version string) (manifest []byte, sigLine string, err error)
	// Binary streams the program of version into dst, reading at most limit bytes.
	Binary(ctx context.Context, version string, dst io.Writer, limit int64) error
}

// Stager downloads and checks a build and leaves it for the updater (step 3 of the brief).
type Stager struct {
	Keys      []Key
	Layout    Layout
	Source    Source
	Plat      Platform
	Installed string
	Channel   string
	Now       func() time.Time
}

// ErrNotNewer is returned when the hub has nothing newer than the installed version.
var ErrNotNewer = errors.New("no newer build")

// Stage fetches version ("" = newest), verifies everything the node can verify and writes the
// bundle to the staging directory, then marks it ready. The updater checks it again.
func (s *Stager) Stage(ctx context.Context, version string) (*Manifest, error) {
	if len(s.Keys) == 0 {
		return nil, ErrNoKeys
	}
	if err := s.Layout.EnsureDir(); err != nil {
		return nil, err
	}
	manifest, sigLine, err := s.Source.Manifest(ctx, version)
	if err != nil {
		return nil, fmt.Errorf("fetch manifest: %w", err)
	}
	if err := Verify(s.Keys, manifest, sigLine); err != nil {
		return nil, err
	}
	m, err := CheckManifest(manifest, s.Plat, s.Installed)
	if err != nil {
		return nil, err
	}
	if version != "" && m.Version != version {
		return nil, fmt.Errorf("asked for version %s, the manifest is for %s", version, m.Version)
	}
	if m.Channel != s.Channel {
		return nil, fmt.Errorf("build is for channel %q, this node follows %q", m.Channel, s.Channel)
	}

	s.Layout.Clear()
	s.Layout.ClearConfirmed()
	dir := s.Layout.Staging()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	fail := func(err error) (*Manifest, error) {
		s.Layout.Clear()
		return nil, err
	}
	bin := filepath.Join(dir, BinaryFile)
	f, err := os.OpenFile(bin, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600) //nolint:gosec // inside our own staging dir
	if err != nil {
		return fail(err)
	}
	h := sha256.New()
	cw := &countWriter{w: io.MultiWriter(f, h)}
	err = s.Source.Binary(ctx, m.Version, cw, m.Size+1)
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		return fail(fmt.Errorf("download program: %w", err))
	}
	if cw.n != m.Size {
		return fail(fmt.Errorf("program is %d bytes, the manifest says %d", cw.n, m.Size))
	}
	if hex.EncodeToString(h.Sum(nil)) != m.SHA256 {
		return fail(errors.New("program does not match the SHA-256 of the manifest"))
	}
	if err := os.WriteFile(filepath.Join(dir, ManifestFile), manifest, 0o600); err != nil {
		return fail(err)
	}
	if err := os.WriteFile(filepath.Join(dir, SigFile), []byte(sigLine), 0o600); err != nil {
		return fail(err)
	}
	if err := s.Layout.MarkReady(m.Version, s.now()); err != nil {
		return fail(err)
	}
	return m, nil
}

func (s *Stager) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

type countWriter struct {
	w io.Writer
	n int64
}

func (c *countWriter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.n += int64(n)
	return n, err
}

// SHA256Hex is the hex SHA-256 of b (used by the signing tool and tests).
func SHA256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
