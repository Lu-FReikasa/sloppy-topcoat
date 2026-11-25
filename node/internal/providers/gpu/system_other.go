//go:build !linux && !windows

package gpu

// NewSystem returns the GPU provider for this OS (unsupported OS: nvidia-smi only).
func NewSystem() GPUProvider { return Multi{NvidiaSMI{}} }
