// Package llamacpp manages the node's own pinned llama.cpp builds (D9): it
// picks the backend for the detected hardware, downloads the official release
// archives from GitHub, verifies them against the SHA-256 values recorded in
// releases.json and unpacks them into the node data dir.
package llamacpp

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
)

//go:embed releases.json
var releasesJSON []byte

// Backends.
const (
	BackendCUDA   = "cuda"
	BackendVulkan = "vulkan"
	BackendCPU    = "cpu"
)

// Asset is one release archive.
type Asset struct {
	Name   string `json:"name"`
	SHA256 string `json:"sha256"`
}

// Build is the set of archives for one OS + backend. All archives are unpacked
// into the same directory (e.g. the CUDA build plus its CUDA runtime libraries).
type Build struct {
	OS      string  `json:"os"`
	Backend string  `json:"backend"`
	Assets  []Asset `json:"assets"`
}

// Release is one pinned llama.cpp release.
type Release struct {
	Builds []Build `json:"builds"`
}

// Manifest lists the pinned releases.
type Manifest struct {
	Default  string             `json:"default"`
	BaseURL  string             `json:"base_url"`
	Releases map[string]Release `json:"releases"`
}

var (
	releaseRe = regexp.MustCompile(`^b[0-9]+$`)
	shaRe     = regexp.MustCompile(`^[0-9a-f]{64}$`)
	assetRe   = regexp.MustCompile(`^[A-Za-z0-9._-]+\.(zip|tar\.gz)$`)
)

// Pinned returns the manifest compiled into this binary.
func Pinned() Manifest {
	m, err := ParseManifest(releasesJSON)
	if err != nil {
		panic("llamacpp: embedded releases.json: " + err.Error())
	}
	return m
}

// ParseManifest decodes and validates a manifest of llama.cpp releases (tags b<number>).
func ParseManifest(b []byte) (Manifest, error) { return ParseManifestFor(b, releaseRe) }

// ParseManifestFor validates the release tags against tagRe instead (another project's pins, e.g.
// stable-diffusion.cpp's master-<n>-<commit>).
func ParseManifestFor(b []byte, tagRe *regexp.Regexp) (Manifest, error) {
	var m Manifest
	if err := json.Unmarshal(b, &m); err != nil {
		return m, err
	}
	if _, ok := m.Releases[m.Default]; !ok {
		return m, fmt.Errorf("default release %q is not listed", m.Default)
	}
	for tag, rel := range m.Releases {
		if !tagRe.MatchString(tag) {
			return m, fmt.Errorf("release %q: bad tag", tag)
		}
		for _, b := range rel.Builds {
			if len(b.Assets) == 0 {
				return m, fmt.Errorf("release %s %s/%s: no assets", tag, b.OS, b.Backend)
			}
			for _, a := range b.Assets {
				if !assetRe.MatchString(a.Name) || !shaRe.MatchString(a.SHA256) {
					return m, fmt.Errorf("release %s: bad asset %q", tag, a.Name)
				}
			}
		}
	}
	return m, nil
}

// Build returns the build for release/goos/backend.
func (m Manifest) Build(release, goos, backend string) (Build, error) {
	rel, ok := m.Releases[release]
	if !ok {
		return Build{}, fmt.Errorf("llama.cpp release %q is not pinned in this node version (known: %v)", release, m.Known())
	}
	for _, b := range rel.Builds {
		if b.OS == goos && b.Backend == backend {
			return b, nil
		}
	}
	return Build{}, fmt.Errorf("llama.cpp %s has no official %s build for %s", release, backend, goos)
}

// Known lists the pinned release tags.
func (m Manifest) Known() []string {
	out := make([]string, 0, len(m.Releases))
	for k := range m.Releases {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// GPUVendor is the vendor of one detected GPU (gpu.Vendor* values).
type GPUVendor = string

// SelectBackend maps the configured backend and the detected GPU vendors to a
// concrete backend: auto -> NVIDIA: cuda, AMD/Intel: vulkan, otherwise cpu.
func SelectBackend(configured string, vendors []GPUVendor) string {
	switch configured {
	case BackendCUDA, BackendVulkan, BackendCPU:
		return configured
	}
	hasVulkan := false
	for _, v := range vendors {
		switch v {
		case "nvidia":
			return BackendCUDA
		case "amd", "intel":
			hasVulkan = true
		}
	}
	if hasVulkan {
		return BackendVulkan
	}
	return BackendCPU
}
