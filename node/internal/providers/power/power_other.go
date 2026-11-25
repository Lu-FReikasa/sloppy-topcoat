//go:build !linux && !windows

package power

import (
	"context"
	"errors"
)

type system struct{}

// NewSystem returns a sleeper that cannot sleep on this OS.
func NewSystem() Sleeper { return system{} }

func (system) Sleep(context.Context) error {
	return errors.New("sleeping is only implemented on Linux and Windows")
}
