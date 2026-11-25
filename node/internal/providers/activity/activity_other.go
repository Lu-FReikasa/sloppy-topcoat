//go:build !linux && !windows

package activity

import (
	"context"
	"time"
)

type system struct{}

// NewSystem returns a provider that knows nothing (no signal counts as activity).
func NewSystem() Provider { return system{} }

func (system) Input(context.Context) (time.Duration, bool) { return 0, false }

func (system) Sample(context.Context, []int) Sample {
	return Sample{InputErr: "activity detection is not implemented on this OS"}
}
