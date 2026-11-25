// Package store persists the node's local state in its data directory:
// identity (node.json), private key (node.key), pinned hub CA (hub-ca.pem),
// cached config (config.json) and the offline event queue (events.jsonl).
package store

import (
	"crypto/ed25519"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// File names inside the data dir.
const (
	IdentityFile = "node.json"
	KeyFile      = "node.key"
	HubCAFile    = "hub-ca.pem"
	ConfigFile   = "config.json"
	QueueFile    = "events.jsonl"
)

// ErrNotPaired is returned when node.json does not exist.
var ErrNotPaired = errors.New("node is not paired (node.json missing); run `topcoat-node pair` first")

// Identity is what pairing produced. The node authenticates with node.key
// (D12); there is no shared secret.
type Identity struct {
	HubURL string `json:"hub_url"`
	NodeID string `json:"node_id"`
	// InsecureHTTP records the explicit opt-in for a plain-http hub (tests, local dev).
	InsecureHTTP bool `json:"insecure_http,omitempty"`
}

// CachedConfig is the last config received from the hub.
type CachedConfig struct {
	ConfigHash string          `json:"config_hash"`
	Config     json.RawMessage `json:"config"`
	ReceivedAt string          `json:"received_at,omitempty"`
}

// Dir is a data directory.
type Dir struct {
	Path string
}

// Open ensures the directory exists with owner-only permissions.
//
// TODO(windows-acl): on Windows the mode bits are ignored; the directory under
// %ProgramData% should get an ACL restricted to SYSTEM and Administrators.
// UNVERIFIED on real hardware (Windows ACL handling).
func Open(path string) (*Dir, error) {
	if path == "" {
		return nil, errors.New("data dir path is empty")
	}
	if err := os.MkdirAll(path, 0o700); err != nil {
		return nil, fmt.Errorf("create data dir: %w", err)
	}
	return &Dir{Path: path}, nil
}

func (d *Dir) file(name string) string { return filepath.Join(d.Path, name) }

// LoadIdentity reads node.json.
func (d *Dir) LoadIdentity() (Identity, error) {
	var id Identity
	b, err := os.ReadFile(d.file(IdentityFile))
	if errors.Is(err, os.ErrNotExist) {
		return id, ErrNotPaired
	}
	if err != nil {
		return id, fmt.Errorf("read identity: %w", err)
	}
	if err := json.Unmarshal(b, &id); err != nil {
		return id, fmt.Errorf("parse identity: %w", err)
	}
	if id.HubURL == "" || id.NodeID == "" {
		return id, fmt.Errorf("identity file %s is incomplete", d.file(IdentityFile))
	}
	return id, nil
}

// HasIdentity reports whether node.json exists.
func (d *Dir) HasIdentity() bool {
	_, err := os.Stat(d.file(IdentityFile))
	return err == nil
}

// SaveIdentity writes node.json with mode 0600. Rewriting an existing file (for example
// `topcoat-node set-hub` run with sudo) keeps its owner, so the service account can still read
// it; a new file belongs to whoever writes it (pairing runs before the data dir is handed over).
func (d *Dir) SaveIdentity(id Identity) error {
	b, err := json.MarshalIndent(id, "", "  ")
	if err != nil {
		return err
	}
	path := d.file(IdentityFile)
	uid, gid, hadOwner := ownerOf(path)
	if err := WriteFileAtomic(path, append(b, '\n')); err != nil {
		return err
	}
	if hadOwner {
		if err := giveTo(path, uid, gid); err != nil {
			return fmt.Errorf("keep the owner of %s: %w", path, err)
		}
	}
	return nil
}

// SavePrivateKey writes the ed25519 private key as PKCS#8 PEM with mode 0600.
func (d *Dir) SavePrivateKey(key ed25519.PrivateKey) error {
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return fmt.Errorf("marshal key: %w", err)
	}
	return WriteFileAtomic(d.file(KeyFile), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}))
}

// LoadPrivateKey reads the ed25519 private key.
func (d *Dir) LoadPrivateKey() (ed25519.PrivateKey, error) {
	b, err := os.ReadFile(d.file(KeyFile))
	if err != nil {
		return nil, fmt.Errorf("read key: %w", err)
	}
	blk, _ := pem.Decode(b)
	if blk == nil {
		return nil, errors.New("key file is not PEM")
	}
	k, err := x509.ParsePKCS8PrivateKey(blk.Bytes)
	if err != nil {
		return nil, fmt.Errorf("parse key: %w", err)
	}
	ek, ok := k.(ed25519.PrivateKey)
	if !ok {
		return nil, errors.New("key is not ed25519")
	}
	return ek, nil
}

// SaveHubCA stores the CA certificate the node trusts for the hub.
func (d *Dir) SaveHubCA(pemBytes []byte) error {
	return WriteFileAtomic(d.file(HubCAFile), pemBytes)
}

// LoadHubCA reads hub-ca.pem; ok=false if the node uses the system trust store.
func (d *Dir) LoadHubCA() (pemBytes []byte, ok bool, err error) {
	b, err := os.ReadFile(d.file(HubCAFile))
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("read hub CA: %w", err)
	}
	return b, true, nil
}

// RemoveHubCA deletes hub-ca.pem (pairing again without a pinned CA).
func (d *Dir) RemoveHubCA() error {
	err := os.Remove(d.file(HubCAFile))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

// LoadConfig reads the cached config; ok=false if none is cached.
func (d *Dir) LoadConfig() (CachedConfig, bool, error) {
	var c CachedConfig
	b, err := os.ReadFile(d.file(ConfigFile))
	if errors.Is(err, os.ErrNotExist) {
		return c, false, nil
	}
	if err != nil {
		return c, false, fmt.Errorf("read cached config: %w", err)
	}
	if err := json.Unmarshal(b, &c); err != nil {
		return c, false, fmt.Errorf("parse cached config: %w", err)
	}
	if len(c.Config) == 0 {
		return c, false, nil
	}
	return c, true, nil
}

// SaveConfig caches the config.
func (d *Dir) SaveConfig(hash string, cfg json.RawMessage, now time.Time) error {
	// Plain Marshal keeps the config bytes as received (MarshalIndent would reformat them).
	b, err := json.Marshal(CachedConfig{ConfigHash: hash, Config: cfg, ReceivedAt: now.UTC().Format(time.RFC3339)})
	if err != nil {
		return err
	}
	return WriteFileAtomic(d.file(ConfigFile), append(b, '\n'))
}

// QueuePath is the path of the offline event queue.
func (d *Dir) QueuePath() string { return d.file(QueueFile) }

// WriteFileAtomic writes data to a temp file (mode 0600) next to path, syncs it and renames it over path.
func WriteFileAtomic(path string, data []byte) error {
	dir := filepath.Dir(path)
	f, err := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return fmt.Errorf("create temp file: %w", err)
	}
	tmp := f.Name()
	cleanup := func() { _ = os.Remove(tmp) }
	if err := f.Chmod(0o600); err != nil && !isChmodUnsupported(err) {
		_ = f.Close()
		cleanup()
		return fmt.Errorf("chmod temp file: %w", err)
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		cleanup()
		return fmt.Errorf("write temp file: %w", err)
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		cleanup()
		return fmt.Errorf("sync temp file: %w", err)
	}
	if err := f.Close(); err != nil {
		cleanup()
		return fmt.Errorf("close temp file: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		cleanup()
		return fmt.Errorf("rename temp file: %w", err)
	}
	return nil
}

// isChmodUnsupported tolerates platforms where File.Chmod is not supported.
func isChmodUnsupported(err error) bool {
	return errors.Is(err, errors.ErrUnsupported)
}
