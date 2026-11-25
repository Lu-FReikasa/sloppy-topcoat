package buildsvc

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"time"
)

// ExecRunner runs the fixed commands for real. UNVERIFIED on real hardware: tested here only
// with harmless commands; the git, docker and bash steps run on the build host.
type ExecRunner struct {
	// Timeout bounds one command (default 30 minutes).
	Timeout time.Duration
}

func (r ExecRunner) Run(ctx context.Context, dir string, env []string, name string, args ...string) (string, error) {
	limit := r.Timeout
	if limit <= 0 {
		limit = 30 * time.Minute
	}
	ctx, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...) //nolint:gosec // only fixed commands of this package
	cmd.Dir, cmd.Env = dir, env
	var out bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &out
	err := cmd.Run()
	text := out.String()
	if len(text) > 1<<20 {
		text = text[len(text)-1<<20:]
	}
	if err != nil {
		return text, fmt.Errorf("%s failed: %w", name, err)
	}
	return text, nil
}
