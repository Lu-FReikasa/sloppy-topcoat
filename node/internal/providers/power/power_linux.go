package power

import (
	"context"
	"fmt"
	"os/exec"
	"strings"
)

type system struct {
	run func(ctx context.Context, name string, args ...string) ([]byte, error)
}

// NewSystem returns the Linux sleeper: `systemctl suspend`. UNVERIFIED on real hardware; the
// node's service account may need a polkit rule for org.freedesktop.login1.suspend.
func NewSystem() Sleeper {
	return system{run: func(ctx context.Context, name string, args ...string) ([]byte, error) {
		return exec.CommandContext(ctx, name, args...).CombinedOutput() //nolint:gosec // fixed tool, fixed arguments
	}}
}

func (s system) Sleep(ctx context.Context) error {
	if out, err := s.run(ctx, "systemctl", "suspend"); err != nil {
		return fmt.Errorf("systemctl suspend: %w: %s (does the service account have the right to suspend? "+
			"see docs/INSTALL-NODE.md \"Energy\")", err, strings.TrimSpace(string(out)))
	}
	return nil
}
