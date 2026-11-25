// Package proc starts and stops child processes (llama-server, llama-bench)
// so that they never outlive the node agent, and measures their memory.
package proc

import (
	"context"
	"errors"
	"io"
	"os/exec"
	"sync"
	"sync/atomic"
	"time"
)

// lowPriority makes every process started from now on run at the lowest scheduling priority
// (D24 step 1: local programs always come first). On by default; config.yield.low_priority.
var lowPriority atomic.Bool

func init() { lowPriority.Store(true) }

// SetLowPriority chooses the priority of processes started afterwards (running ones keep theirs).
func SetLowPriority(on bool) { lowPriority.Store(on) }

// LowPriority reports the current setting.
func LowPriority() bool { return lowPriority.Load() }

// Process is a started child process.
type Process interface {
	PID() int
	// Done is closed when the process exited; Err then holds the exit error.
	Done() <-chan struct{}
	Err() error
	// Stop asks the process to exit (SIGTERM on Linux; on Windows there is no
	// graceful signal for console-less children, so it is killed) and kills it
	// after timeout.
	Stop(timeout time.Duration) error
	// Kill ends the process at once, without asking it to exit (a server that is still loading
	// its model does not react to a polite request for minutes). It does not wait: Done closes
	// when the operating system has finished tearing the process down.
	Kill() error
	// RSSMB is the resident memory in MB (0 if unknown).
	RSSMB() int64
	// Suspend freezes the process at the point it has reached (SIGSTOP on Linux,
	// NtSuspendProcess on Windows), Resume continues it there (D24 step 2). Both are
	// idempotent: suspending a frozen process or resuming a running one does nothing.
	Suspend() error
	Resume() error
}

// Starter starts a program with arguments; output goes to out.
type Starter func(ctx context.Context, bin string, args []string, dir string, out io.Writer) (Process, error)

type osProcess struct {
	cmd  *exec.Cmd
	done chan struct{}
	err  error
	sys  sysState

	mu        sync.Mutex
	suspended bool
}

// Start is the real Starter.
func Start(_ context.Context, bin string, args []string, dir string, out io.Writer) (Process, error) {
	cmd := exec.Command(bin, args...) //nolint:gosec // fixed binary from the verified runtime, fixed argument template
	cmd.Dir = dir
	cmd.Stdout = out
	cmd.Stderr = out
	low := lowPriority.Load()
	prepare(cmd, low)
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	p := &osProcess{cmd: cmd, done: make(chan struct{})}
	p.sys = afterStart(cmd, low)
	go func() {
		p.err = cmd.Wait()
		close(p.done)
	}()
	return p, nil
}

func (p *osProcess) PID() int              { return p.cmd.Process.Pid }
func (p *osProcess) Done() <-chan struct{} { return p.done }
func (p *osProcess) Err() error {
	select {
	case <-p.done:
		return p.err
	default:
		return nil
	}
}

// Kill implements Process.
func (p *osProcess) Kill() error {
	select {
	case <-p.done:
		return nil
	default:
	}
	_ = p.Resume() // a stopped process is killed too, but let it run to see the signal at once
	if err := p.cmd.Process.Kill(); err != nil && !gone(err) {
		return err
	}
	return nil
}

// Suspend implements Process.
func (p *osProcess) Suspend() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.suspended {
		return nil
	}
	select {
	case <-p.done:
		return nil // nothing left to freeze
	default:
	}
	if err := suspend(p.cmd.Process.Pid); err != nil {
		return err
	}
	p.suspended = true
	return nil
}

// Resume implements Process.
func (p *osProcess) Resume() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.suspended {
		return nil
	}
	if err := resume(p.cmd.Process.Pid); err != nil {
		select {
		case <-p.done:
		default:
			return err
		}
	}
	p.suspended = false
	return nil
}

func (p *osProcess) Stop(timeout time.Duration) error {
	select {
	case <-p.done:
		return nil
	default:
	}
	// A stopped process does not see SIGTERM until it is continued.
	_ = p.Resume()
	_ = terminate(p.cmd)
	t := time.NewTimer(timeout)
	defer t.Stop()
	select {
	case <-p.done:
		return nil
	case <-t.C:
	}
	if err := p.cmd.Process.Kill(); err != nil && !gone(err) {
		return err
	}
	// A process that holds many GB is torn down by the kernel for a while after it was
	// killed; Done closes when that is over.
	select {
	case <-p.done:
		return nil
	case <-time.After(killWait):
		return errors.New("the process did not end after it was killed")
	}
}

// killWait is how long Stop waits for the kernel to finish a killed process (releasing 18 GB
// of a large model takes seconds, not milliseconds).
const killWait = 2 * time.Minute

func (p *osProcess) RSSMB() int64 { return rssMB(p.cmd.Process.Pid) }

// CommandContext builds an exec.Cmd for short-lived tools (llama-bench) with
// the same kill-with-parent protection; canceling ctx kills the process.
func CommandContext(ctx context.Context, bin string, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, bin, args...) //nolint:gosec // fixed binary from the verified runtime
	prepare(cmd, lowPriority.Load())
	return cmd
}
