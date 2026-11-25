// Package update is the node side of the signed update channel (E2-06, D50): the signed
// manifest, the key handling, the staging done by the node and the apply logic of the
// privileged updater. It is plain Go and tested on every OS; the operating system calls of the
// updater sit behind the Host interface.
package update

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

const (
	// FormatVersion is the manifest format this code reads and writes.
	FormatVersion = 1
	// Product is the only product a manifest may name.
	Product = "topcoat-node"
	// SigScheme starts the signature line.
	SigScheme = "topcoat-sig-v1"
	// SignedPrefix is put in front of the manifest bytes before signing (domain separation).
	SignedPrefix = "topcoat-update-v1\n"

	// MaxManifestBytes bounds a manifest or signature file.
	MaxManifestBytes = 16 << 10
	// MaxBinaryBytes bounds the program file of a build.
	MaxBinaryBytes = 256 << 20
	// MaxKeys is how many public keys a node build may carry (main and reserve).
	MaxKeys = 2
)

// Manifest describes one build. It is signed exactly as written: nobody re-serializes it.
type Manifest struct {
	Format  int    `json:"format"`
	Product string `json:"product"`
	Channel string `json:"channel"`
	Version string `json:"version"`
	OS      string `json:"os"`
	Arch    string `json:"arch"`
	Size    int64  `json:"size"`
	SHA256  string `json:"sha256"`
	Created string `json:"created"`
}

var (
	channelRe = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}$`)
	platRe    = regexp.MustCompile(`^[a-z0-9_]{1,16}$`)
	sha256Re  = regexp.MustCompile(`^[0-9a-f]{64}$`)
	versionRe = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]{1,32})?$`)
)

// ValidChannel reports whether name is a legal channel name.
func ValidChannel(name string) bool { return channelRe.MatchString(name) }

// ParseManifest decodes and validates manifest bytes. Unknown fields are refused: a signed
// document is read strictly.
func ParseManifest(b []byte) (*Manifest, error) {
	if len(b) == 0 || len(b) > MaxManifestBytes {
		return nil, fmt.Errorf("manifest: size %d is not between 1 and %d bytes", len(b), MaxManifestBytes)
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	var m Manifest
	if err := dec.Decode(&m); err != nil {
		return nil, fmt.Errorf("manifest: %w", err)
	}
	if dec.More() {
		return nil, errors.New("manifest: trailing data")
	}
	if err := m.Validate(); err != nil {
		return nil, err
	}
	return &m, nil
}

// Validate checks every field.
func (m *Manifest) Validate() error {
	switch {
	case m.Format != FormatVersion:
		return fmt.Errorf("manifest: format %d is not supported (want %d)", m.Format, FormatVersion)
	case m.Product != Product:
		return fmt.Errorf("manifest: product %q is not %q", m.Product, Product)
	case !channelRe.MatchString(m.Channel):
		return fmt.Errorf("manifest: channel %q is not valid", m.Channel)
	case !versionRe.MatchString(m.Version):
		return fmt.Errorf("manifest: version %q is not MAJOR.MINOR.PATCH[-pre]", m.Version)
	case !platRe.MatchString(m.OS) || !platRe.MatchString(m.Arch):
		return fmt.Errorf("manifest: platform %q/%q is not valid", m.OS, m.Arch)
	case m.Size < 1 || m.Size > MaxBinaryBytes:
		return fmt.Errorf("manifest: size %d is not between 1 and %d", m.Size, MaxBinaryBytes)
	case !sha256Re.MatchString(m.SHA256):
		return errors.New("manifest: sha256 must be 64 lower-case hex digits")
	}
	return nil
}

// Key is a trusted public key.
type Key struct {
	ID  string
	Pub ed25519.PublicKey
}

// KeyID is the first 16 hex digits of the SHA-256 of the public key.
func KeyID(pub ed25519.PublicKey) string {
	sum := sha256.Sum256(pub)
	return hex.EncodeToString(sum[:8])
}

// ParseKeys reads comma-separated base64 public keys (the build-time setting). An empty string
// is no key at all.
func ParseKeys(s string) ([]Key, error) {
	var keys []Key
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		raw, err := base64.StdEncoding.DecodeString(part)
		if err != nil || len(raw) != ed25519.PublicKeySize {
			return nil, fmt.Errorf("update key %q is not a base64 ed25519 public key", part)
		}
		pub := ed25519.PublicKey(raw)
		keys = append(keys, Key{ID: KeyID(pub), Pub: pub})
	}
	if len(keys) > MaxKeys {
		return nil, fmt.Errorf("at most %d update keys are allowed, got %d", MaxKeys, len(keys))
	}
	return keys, nil
}

// KeyIDs lists the ids of keys.
func KeyIDs(keys []Key) []string {
	ids := make([]string, 0, len(keys))
	for _, k := range keys {
		ids = append(ids, k.ID)
	}
	return ids
}

func signedMessage(manifest []byte) []byte {
	return append([]byte(SignedPrefix), manifest...)
}

// Sign signs manifest bytes and returns the signature line.
func Sign(priv ed25519.PrivateKey, manifest []byte) string {
	pub, _ := priv.Public().(ed25519.PublicKey)
	sig := ed25519.Sign(priv, signedMessage(manifest))
	return SigScheme + " " + KeyID(pub) + " " + base64.StdEncoding.EncodeToString(sig)
}

// ErrNoKeys is returned when a build carries no update key.
var ErrNoKeys = errors.New("this build carries no update key, so it cannot verify an update")

// Verify checks the signature line of manifest against keys.
func Verify(keys []Key, manifest []byte, sigLine string) error {
	if len(keys) == 0 {
		return ErrNoKeys
	}
	fields := strings.Fields(strings.TrimSpace(sigLine))
	if len(fields) != 3 || fields[0] != SigScheme {
		return errors.New("signature: not a " + SigScheme + " line")
	}
	sig, err := base64.StdEncoding.DecodeString(fields[2])
	if err != nil || len(sig) != ed25519.SignatureSize {
		return errors.New("signature: malformed")
	}
	for _, k := range keys {
		if k.ID != fields[1] {
			continue
		}
		if !ed25519.Verify(k.Pub, signedMessage(manifest), sig) {
			return errors.New("signature: does not match the manifest")
		}
		return nil
	}
	return fmt.Errorf("signature: key %s is not trusted by this node", fields[1])
}

// Version is a parsed MAJOR.MINOR.PATCH[-pre].
type Version struct {
	Major, Minor, Patch int
	Pre                 string
}

// ParseVersion parses a version string.
func ParseVersion(s string) (Version, error) {
	m := versionRe.FindStringSubmatch(s)
	if m == nil {
		return Version{}, fmt.Errorf("version %q is not MAJOR.MINOR.PATCH[-pre]", s)
	}
	var v Version
	var err error
	if v.Major, err = strconv.Atoi(m[1]); err != nil {
		return Version{}, err
	}
	if v.Minor, err = strconv.Atoi(m[2]); err != nil {
		return Version{}, err
	}
	if v.Patch, err = strconv.Atoi(m[3]); err != nil {
		return Version{}, err
	}
	v.Pre = strings.TrimPrefix(m[4], "-")
	return v, nil
}

// Compare returns -1, 0 or 1. A pre-release is older than the release of the same triple.
func (v Version) Compare(o Version) int {
	for _, p := range [][2]int{{v.Major, o.Major}, {v.Minor, o.Minor}, {v.Patch, o.Patch}} {
		if p[0] != p[1] {
			if p[0] < p[1] {
				return -1
			}
			return 1
		}
	}
	switch {
	case v.Pre == o.Pre:
		return 0
	case v.Pre == "":
		return 1
	case o.Pre == "":
		return -1
	case v.Pre < o.Pre:
		return -1
	}
	return 1
}

// Newer reports whether candidate is a strictly newer version than installed. An installed
// version that is not a release number (a development build) counts as 0.0.0, so any release is
// newer.
func Newer(candidate, installed string) (bool, error) {
	c, err := ParseVersion(candidate)
	if err != nil {
		return false, err
	}
	i, err := ParseVersion(installed)
	if err != nil {
		i = Version{}
	}
	return c.Compare(i) > 0, nil
}

// Platform is the operating system and architecture a build is for.
type Platform struct{ OS, Arch string }

// Check verifies a bundle completely: signature, manifest, platform, version and the bytes of
// the program. It returns the manifest. Both the node (before staging) and the updater (before
// applying) call it, each on the bytes it holds.
func Check(keys []Key, manifest []byte, sigLine string, binary []byte, plat Platform, installed string) (*Manifest, error) {
	if err := Verify(keys, manifest, sigLine); err != nil {
		return nil, err
	}
	m, err := CheckManifest(manifest, plat, installed)
	if err != nil {
		return nil, err
	}
	if int64(len(binary)) != m.Size {
		return nil, fmt.Errorf("program is %d bytes, the manifest says %d", len(binary), m.Size)
	}
	sum := sha256.Sum256(binary)
	if hex.EncodeToString(sum[:]) != m.SHA256 {
		return nil, errors.New("program does not match the SHA-256 of the manifest")
	}
	return m, nil
}

// CheckManifest parses manifest bytes (already verified) and checks platform and version.
func CheckManifest(manifest []byte, plat Platform, installed string) (*Manifest, error) {
	m, err := ParseManifest(manifest)
	if err != nil {
		return nil, err
	}
	if m.OS != plat.OS || m.Arch != plat.Arch {
		return nil, fmt.Errorf("build is for %s/%s, this machine is %s/%s", m.OS, m.Arch, plat.OS, plat.Arch)
	}
	newer, err := Newer(m.Version, installed)
	if err != nil {
		return nil, err
	}
	if !newer {
		return nil, fmt.Errorf("version %s is not newer than the installed %s", m.Version, installed)
	}
	return m, nil
}
