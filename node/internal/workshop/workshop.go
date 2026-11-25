// Package workshop is the dev node's part of the dev workshop (E6-01, ARCHITECTURE D52): clones of
// registered repositories and the named repo.* capabilities the assistant may use on them.
//
// There is no shell. Every capability is a fixed operation with a schema-checked input; git runs
// with a fixed argument list, a scrubbed environment and a protocol allow-list. The worktrees are
// scratch (disposable, shown as a diff), so changing them needs no confirmation; leaving the
// workshop (a push) is a separate, confirmed step that does not live in this package yet.
//
// The hub hands a repository over with each call (URL, branch, test template, token) in a Spec in
// the context. The node never stores a token: it reaches git through the process environment of
// that one call and the remote URL in the clone stays free of credentials.
package workshop

import (
	"context"
	"encoding/base64"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/tools"
)

// Spec is what the hub knows about one repository and hands over with a call. Token may be empty
// (a public repository). It never appears in a log line, an error or an output.
type Spec struct {
	URL      string `json:"url"`
	Branch   string `json:"branch"`
	Template string `json:"template,omitempty"`
	Token    string `json:"token,omitempty"`
	// Local marks a repository whose origin lives in this workshop (D76): the clone comes from
	// the node's own origin directory for it, never from a path the hub names; URL is empty or a
	// remote it may be published to.
	Local bool `json:"local,omitempty"`
}

type specKey struct{}

// WithSpec puts the repository of the current call into the context.
func WithSpec(ctx context.Context, s Spec) context.Context {
	return context.WithValue(ctx, specKey{}, s)
}

func specOf(ctx context.Context) (Spec, bool) {
	s, ok := ctx.Value(specKey{}).(Spec)
	return s, ok
}

// Options configure a Workshop.
type Options struct {
	// Dir is where the clones live (created when missing).
	Dir string
	// AllowedHosts are the only hosts a repository URL may name (default github.com).
	AllowedHosts []string
	// MaxRepoMB bounds the size of a clone (default protocol.DefaultWorkshopRepoMB).
	MaxRepoMB int
	// Browser drives the browser.* capabilities (E6-02); nil = they are not offered. BrowserIdle ends a
	// session that was not used for that long; 0 = 5 minutes.
	Browser     BrowserRunner
	BrowserIdle time.Duration
	// Runner runs repo.test; nil = repo.test is not offered. RunnerConfig carries the images and
	// limits from config.tools.workshop.runner.
	Runner       Runner
	RunnerConfig protocol.WorkshopRunner
	// Templates maps a test template to the command it runs (default DefaultTemplates).
	Templates map[string]Template
	// GitBin is the git program (default "git").
	GitBin string
	// allowFileURLs lets tests clone from a local directory; it has no configuration path.
	allowFileURLs bool
	// Now is the clock (tests).
	Now func() time.Time
}

// Workshop holds the clones of a dev node.
type Workshop struct {
	o    Options
	dir  string
	home string // an empty HOME for git, so no user config is read

	mu    sync.Mutex
	locks map[string]*sync.Mutex // one operation at a time per repository

	// background test runs (repo.test_start) live here, in memory: a restart of the node ends them
	bg     context.Context
	runsMu sync.Mutex
	runs   map[string]*testRun

	// the one browser session of this node (E6-02)
	browserMu sync.Mutex
	browser   *openBrowser
}

// New prepares the directories and returns the workshop.
func New(o Options) (*Workshop, error) {
	if o.Dir == "" || !filepath.IsAbs(o.Dir) {
		return nil, fmt.Errorf("workshop: the directory must be an absolute path")
	}
	if len(o.AllowedHosts) == 0 {
		o.AllowedHosts = []string{"github.com"}
	}
	if o.MaxRepoMB <= 0 {
		o.MaxRepoMB = protocol.DefaultWorkshopRepoMB
	}
	if o.GitBin == "" {
		o.GitBin = "git"
	}
	if o.Templates == nil {
		o.Templates = DefaultTemplates
	}
	if o.Now == nil {
		o.Now = time.Now
	}
	w := &Workshop{o: o, dir: o.Dir, home: filepath.Join(o.Dir, ".home"), locks: map[string]*sync.Mutex{}, bg: context.Background(), runs: map[string]*testRun{}}
	for _, d := range []string{w.dir, w.home} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			return nil, fmt.Errorf("workshop: %w", err)
		}
	}
	return w, nil
}

// Registry returns the capabilities of the workshop.
func (w *Workshop) Registry() []capability.Capability { return w.capabilities() }

var (
	repoNameRe = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,62}$`)
	branchRe   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$`)
	repoPathRe = regexp.MustCompile(`^/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+?(\.git)?$`)
)

func inputErr(format string, a ...any) error {
	return &tools.InputError{Msg: fmt.Sprintf(format, a...)}
}

// checkSpec refuses a repository the workshop must not touch: another scheme than https, a user
// name or password in the URL, a host outside the allow-list, a branch that is not a plain name.
func (w *Workshop) checkSpec(s Spec) error {
	if s.Local {
		return checkBranch(s.Branch)
	}
	u, err := url.Parse(s.URL)
	if err != nil || s.URL == "" {
		return inputErr("the repository URL is not valid")
	}
	if w.o.allowFileURLs && u.Scheme == "file" {
		// tests only
	} else {
		if u.Scheme != "https" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Port() != "" {
			return inputErr("the repository URL must be a plain https URL without credentials")
		}
		if !slices.Contains(w.o.AllowedHosts, strings.ToLower(u.Hostname())) {
			return inputErr("the host %q is not on this node's list of allowed hosts (config.tools.workshop.allowed_hosts)", u.Hostname())
		}
		if !repoPathRe.MatchString(u.Path) {
			return inputErr("the repository URL must look like https://host/owner/name")
		}
	}
	return checkBranch(s.Branch)
}

// checkBranch refuses a branch that is not a plain name.
func checkBranch(b string) error {
	if !branchRe.MatchString(b) || strings.Contains(b, "..") || strings.HasSuffix(b, "/") || strings.HasSuffix(b, ".lock") {
		return inputErr("the branch %q is not a plain branch name", b)
	}
	return nil
}

// checkRemote refuses a remote a local repository may be cloned from or published to: the same
// rules as for an https repository of the hub (allowed hosts, no credentials in the URL).
func (w *Workshop) checkRemote(s Spec) error {
	remote := s
	remote.Local = false
	if remote.Branch == "" {
		remote.Branch = "main"
	}
	return w.checkSpec(remote)
}

// originDir is where the origin of a local repository lives in this workshop (D76): computed from
// the name, never named by the hub, and not a name a clone can take (clone names start with a
// letter or digit).
func (w *Workshop) originDir(name string) (string, error) {
	if !repoNameRe.MatchString(name) {
		return "", inputErr("repo must be a short name: lower-case letters, digits, . _ -")
	}
	return filepath.Join(w.dir, ".origin", name+".git"), nil
}

// localOrigin returns the origin of an existing local repository.
func (w *Workshop) localOrigin(name string) (string, error) {
	d, err := w.originDir(name)
	if err != nil {
		return "", err
	}
	if st, err := os.Stat(d); err != nil || !st.IsDir() {
		return "", inputErr("there is no local repository %q in this workshop", name)
	}
	return d, nil
}

// authFor builds the git configuration entry for one call: the token goes into the environment of
// the git process (not its arguments, which other users can read) and only for the repository's
// host. nil = no token.
func authFor(s Spec) *authHeader {
	if s.Token == "" {
		return nil
	}
	u, err := url.Parse(s.URL)
	if err != nil || u.Scheme != "https" {
		return nil
	}
	tok := s.Token
	cred := base64.StdEncoding.EncodeToString([]byte("x-access-token:" + tok))
	return &authHeader{
		key:     "http.https://" + u.Hostname() + "/.extraHeader",
		value:   "Authorization: Basic " + cred,
		secrets: []string{cred, tok},
	}
}

// repoDir returns the clone of a repository name, which must be a plain name.
func (w *Workshop) repoDir(name string) (string, error) {
	if !repoNameRe.MatchString(name) {
		return "", inputErr("repo must be a short name: lower-case letters, digits, . _ -")
	}
	return filepath.Join(w.dir, name), nil
}

// clone returns the directory of an existing clone or says it has to be synced first.
func (w *Workshop) clone(name string) (string, error) {
	d, err := w.repoDir(name)
	if err != nil {
		return "", err
	}
	if st, err := os.Stat(filepath.Join(d, ".git")); err != nil || !st.IsDir() {
		return "", inputErr("repository %q is not cloned yet: run repo.sync first", name)
	}
	return d, nil
}
