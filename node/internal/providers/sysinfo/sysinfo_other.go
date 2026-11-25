//go:build !linux && !windows

package sysinfo

import (
	"context"
	"fmt"
	"runtime"

	"topcoat/node/internal/providers"
)

// System is a placeholder for unsupported operating systems.
type System struct{}

// NewSystem returns the placeholder provider.
func NewSystem() *System { return &System{} }

// Info implements Provider.
func (*System) Info(context.Context) (Info, error) {
	return Info{CPUModel: "unknown", Cores: runtime.NumCPU()}, fmt.Errorf("sysinfo %s: %w", runtime.GOOS, providers.ErrNotImplemented)
}
