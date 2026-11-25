//go:build !linux && !windows

package watcher

import (
	"fmt"

	"topcoat/node/internal/providers"
)

func listProcesses() ([]Proc, error) {
	return nil, fmt.Errorf("process watcher: %w", providers.ErrNotImplemented)
}
