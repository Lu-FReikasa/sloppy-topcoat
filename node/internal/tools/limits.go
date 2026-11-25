package tools

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"time"
	"unicode/utf8"
)

// Limits of one capability call. They keep a tool node small (SPEC: ≤ 30 MB idle) and keep the
// output short enough to read and to put in front of a model.
const (
	// DefaultMaxOutputBytes is the most text one call returns.
	DefaultMaxOutputBytes = 64 << 10
	// HardMaxOutputBytes is the ceiling an admin can raise the limit to.
	HardMaxOutputBytes = 256 << 10
	// MaxLogLines is the most log lines one call asks for.
	MaxLogLines = 500
	// DefaultLogLines is used when a call does not say.
	DefaultLogLines = 100
	// DefaultTimeout bounds one call.
	DefaultTimeout = 20 * time.Second
	// MaxFileBytes is the most of a compose / env file that is read.
	MaxFileBytes = 256 << 10
)

// tailBytes keeps the newest part of a log that is over max bytes: it cuts at a line start and
// says how much was left out.
func tailBytes(s string, max int) (string, bool) {
	if len(s) <= max {
		return s, false
	}
	cut := len(s) - max
	if i := strings.IndexByte(s[cut:], '\n'); i >= 0 {
		cut += i + 1
	} else {
		for cut < len(s) && !utf8.RuneStart(s[cut]) {
			cut++
		}
	}
	return fmt.Sprintf("[… %d earlier bytes left out]\n%s", cut, s[cut:]), true
}

// headBytes keeps the start of a text (a file) that is over max bytes, cut at a rune boundary.
func headBytes(s string, max int) (string, bool) {
	if len(s) <= max {
		return s, false
	}
	cut := max
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut] + fmt.Sprintf("\n[… cut after %d bytes]", cut), true
}

// Commander runs the few external programs a capability needs. It is an interface so the tests
// use a fake; the real one runs fixed binaries with fixed argument lists, never through a shell.
type Commander interface {
	Run(ctx context.Context, bin string, args ...string) ([]byte, error)
}

// allowedBinaries are the only programs the real Commander starts.
var allowedBinaries = map[string]bool{"journalctl": true, "systemctl": true}

// ExecCommander starts the allowed binaries.
type ExecCommander struct {
	// MaxOutput caps what is kept of the output (default DefaultMaxOutputBytes*2).
	MaxOutput int
}

type cappedBuffer struct {
	buf       bytes.Buffer
	max       int
	truncated bool
}

func (c *cappedBuffer) Write(p []byte) (int, error) {
	room := c.max - c.buf.Len()
	if room <= 0 {
		c.truncated = true
		return len(p), nil
	}
	if len(p) > room {
		c.buf.Write(p[:room])
		c.truncated = true
		return len(p), nil
	}
	return c.buf.Write(p)
}

// Run implements Commander.
func (e ExecCommander) Run(ctx context.Context, bin string, args ...string) ([]byte, error) {
	if !allowedBinaries[bin] {
		return nil, fmt.Errorf("the capability layer does not run %q", bin)
	}
	path, err := exec.LookPath(bin)
	if err != nil {
		return nil, fmt.Errorf("%s is not installed on this node", bin)
	}
	max := e.MaxOutput
	if max <= 0 {
		max = 2 * DefaultMaxOutputBytes
	}
	out := &cappedBuffer{max: max}
	var errb cappedBuffer
	errb.max = 4 << 10
	cmd := exec.CommandContext(ctx, path, args...) //nolint:gosec // a fixed binary from allowedBinaries with arguments the capabilities validated
	cmd.Stdout, cmd.Stderr = out, &errb
	cmd.Env = []string{"LC_ALL=C", "SYSTEMD_COLORS=0", "PATH=/usr/sbin:/usr/bin:/sbin:/bin"}
	if err := cmd.Run(); err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return out.buf.Bytes(), fmt.Errorf("%s did not finish in time", bin)
		}
		msg := strings.TrimSpace(errb.buf.String())
		if msg == "" {
			msg = err.Error()
		}
		return out.buf.Bytes(), fmt.Errorf("%s: %s", bin, msg)
	}
	return out.buf.Bytes(), nil
}
