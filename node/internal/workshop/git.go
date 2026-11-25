package workshop

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"
)

const (
	gitTimeout     = 120 * time.Second
	gitOutputLimit = 4 << 20 // git may print a lot (a diff of a big change); callers cut it down
)

// git runs one git command with a fixed argument list. dir is the working directory ("" = the
// workshop directory). The environment is built from nothing: no user or system configuration, no
// prompts, only https (and file in tests), no hooks.
func (w *Workshop) git(ctx context.Context, dir string, auth *authHeader, stdin string, args ...string) (string, error) {
	if dir == "" {
		dir = w.dir
	}
	ctx, cancel := context.WithTimeout(ctx, gitTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, w.o.GitBin, args...) //nolint:gosec // G204: the git binary from the node's config, argument lists built in this package, no shell
	cmd.Dir = dir
	protocols := "https"
	if w.o.allowFileURLs || localCall(ctx) {
		// a local repository (D76) is cloned from and pushed to its origin in this workshop
		protocols = "https:file"
	}
	env := []string{
		"PATH=" + os.Getenv("PATH"),
		"HOME=" + w.home,
		"LC_ALL=C",
		"GIT_CONFIG_NOSYSTEM=1",
		"GIT_CONFIG_GLOBAL=/dev/null",
		"GIT_TERMINAL_PROMPT=0",
		"GIT_ALLOW_PROTOCOL=" + protocols,
		"GIT_OPTIONAL_LOCKS=0",
	}
	// no hooks, no external diff or textconv drivers, whatever a clone's .gitattributes asks for
	config := [][2]string{{"core.hooksPath", "/dev/null"}, {"core.fsmonitor", "false"}}
	if auth != nil {
		config = append(config, [2]string{auth.key, auth.value})
	}
	env = append(env, fmt.Sprintf("GIT_CONFIG_COUNT=%d", len(config)))
	for i, kv := range config {
		env = append(env, fmt.Sprintf("GIT_CONFIG_KEY_%d=%s", i, kv[0]), fmt.Sprintf("GIT_CONFIG_VALUE_%d=%s", i, kv[1]))
	}
	cmd.Env = env
	if stdin != "" {
		cmd.Stdin = strings.NewReader(stdin)
	}
	out := &limitedBuffer{limit: gitOutputLimit}
	var stderr bytes.Buffer
	cmd.Stdout = out
	cmd.Stderr = &stderr
	err := cmd.Run()
	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return "", fmt.Errorf("git %s did not finish in %s", args[0], gitTimeout)
		}
		var ee *exec.ExitError
		msg := strings.TrimSpace(stderr.String())
		if errors.As(err, &ee) || msg != "" {
			// stderr can hold the URL; it never holds the token because the token is not in the
			// arguments, but a server may echo it, so the message is cut and scrubbed anyway
			code := -1
			if ee != nil {
				code = ee.ExitCode()
			}
			return out.String(), &gitError{msg: auth.scrub(msg), exit: code}
		}
		return "", fmt.Errorf("git: %w", err)
	}
	return out.String(), nil
}

// localCall: the call works on a local repository, whose origin is a directory of this workshop.
func localCall(ctx context.Context) bool {
	s, ok := specOf(ctx)
	return ok && s.Local
}

// gitError is a refusal or failure git reported.
type gitError struct {
	msg  string
	exit int
}

func (e *gitError) Error() string {
	m := e.msg
	if len(m) > 600 {
		m = m[:600] + "…"
	}
	if m == "" {
		m = "git failed"
	}
	return m
}

// authHeader is the http.extraHeader of one call, scoped to the repository's host.
type authHeader struct {
	key, value string
	secrets    []string
}

// scrub removes the credential from a message.
func (h *authHeader) scrub(msg string) string {
	if h == nil {
		return msg
	}
	for _, s := range h.secrets {
		if s != "" {
			msg = strings.ReplaceAll(msg, s, "«masked»")
		}
	}
	return msg
}

// limitedBuffer keeps at most limit bytes and remembers that it dropped the rest.
type limitedBuffer struct {
	buf     bytes.Buffer
	limit   int
	dropped bool
}

func (b *limitedBuffer) Write(p []byte) (int, error) {
	room := b.limit - b.buf.Len()
	if room <= 0 {
		b.dropped = true
		return len(p), nil
	}
	if len(p) > room {
		b.buf.Write(p[:room])
		b.dropped = true
		return len(p), nil
	}
	return b.buf.Write(p)
}

func (b *limitedBuffer) String() string { return b.buf.String() }
