// Package sdcpp pins the node's stable-diffusion.cpp builds (D126): the image runner's server
// (sd-server) comes, like llama-server, as an official release archive verified by SHA-256 and
// unpacked by the llama.cpp installer into the node data dir.
package sdcpp

import (
	_ "embed"
	"regexp"

	"topcoat/node/internal/llamacpp"
)

//go:embed releases.json
var releasesJSON []byte

// the release tags of stable-diffusion.cpp: master-<build number>-<short commit>
var tagRe = regexp.MustCompile(`^master-[0-9]+-[0-9a-f]{7,40}$`)

// Pinned returns the manifest compiled into this binary.
func Pinned() llamacpp.Manifest {
	m, err := llamacpp.ParseManifestFor(releasesJSON, tagRe)
	if err != nil {
		panic("sdcpp: embedded releases.json: " + err.Error())
	}
	return m
}

// NewInstaller returns an installer for the pinned builds below root (<data dir>/runtime-sd).
func NewInstaller(root string) *llamacpp.Installer {
	in := llamacpp.NewInstaller(root)
	in.Manifest = Pinned()
	in.Executables = []string{"sd-server"}
	return in
}

// SelectBackend picks the build for the detected GPUs on goos. Like llama.cpp, except that there
// is no official Linux CUDA build: an NVIDIA GPU on Linux uses Vulkan.
func SelectBackend(goos, configured string, vendors []llamacpp.GPUVendor) string {
	b := llamacpp.SelectBackend(configured, vendors)
	if b == llamacpp.BackendCUDA && goos != "windows" {
		return llamacpp.BackendVulkan
	}
	return b
}
