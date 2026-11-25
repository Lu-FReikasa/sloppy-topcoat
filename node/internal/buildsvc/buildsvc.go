// Package buildsvc is the build service of a node (NS-01, D80): it builds a release from a
// pinned commit and hands the files to the node, which uploads them to the hub. It runs as its
// own OS user (rootless Docker, its own directory), talks to the node over the service bus, and
// has exactly these named steps: fetch, node_release, hub_image, node_container_image,
// smoke_test. There is no free-form command line anywhere: every command is fixed in this file
// and only validated values go into its arguments. It never signs anything and holds no key.
package buildsvc

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
)

// Action names, the same as the capabilities the hub knows.
const (
	ActionFetch      = "build.fetch"
	ActionNode       = "build.node_release"
	ActionHubImage   = "build.hub_image"
	ActionNodeImage  = "build.node_container_image"
	ActionSmokeTest  = "build.smoke_test"
	maxLog           = 60000
	maxChanges       = 4000
	maxArtifactBytes = 512 << 20
)

// Actions lists what the service declares to the node.
var Actions = []string{ActionFetch, ActionNode, ActionHubImage, ActionNodeImage, ActionSmokeTest}

var (
	commitRe  = regexp.MustCompile(`^[0-9a-f]{40}$`)
	refRe     = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$`)
	versionRe = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]{1,32})?$`)
	buildIDRe = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	imageRe   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}$`)
	digestRe  = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	nameRe    = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`)
	sourceRe  = regexp.MustCompile(`^(release_repo|dev_mirror)$`)
)

// Runner runs one fixed command and returns its combined output. The real one is ExecRunner.
type Runner interface {
	Run(ctx context.Context, dir string, env []string, name string, args ...string) (string, error)
}

// Host is what the node does for the service: it uploads a finished file to the hub with the
// node's own credentials (the service has none).
type Host interface {
	Upload(ctx context.Context, u Upload) error
}

// Upload names a file below the service's out directory.
type Upload struct {
	BuildID string `json:"build_id"`
	Name    string `json:"name"`
	OS      string `json:"os"`
	Arch    string `json:"arch"`
	Path    string `json:"path"`
}

// Config is the service's own setting; none of it comes from the hub's call.
type Config struct {
	// Work is the service's own directory (mirrors, clones, outputs).
	Work string
	// Sources maps a source key to the repository URL (a path or an https URL).
	Sources map[string]string
	// UpdateKeys are the hub's public update keys, built into the node release.
	UpdateKeys string
	Runner     Runner
	Host       Host
}

// Service runs the steps, one at a time.
type Service struct {
	cfg  Config
	busy sync.Mutex
	// lastSrc is the checkout of the image built last: its smoke test script is the one that runs
	lastSrc string
}

// New returns a service; Work, Runner and Host are required.
func New(cfg Config) (*Service, error) {
	if cfg.Work == "" || cfg.Runner == nil || cfg.Host == nil {
		return nil, errors.New("buildsvc: work directory, runner and host are required")
	}
	return &Service{cfg: cfg}, nil
}

// ErrBusy is returned while another step is running: one build at a time.
var ErrBusy = errors.New("another build step is running")

// Handle runs one action. The input is checked strictly; an unknown field is an error.
func (s *Service) Handle(ctx context.Context, action string, input json.RawMessage) (json.RawMessage, error) {
	if err := Validate(action, input); err != nil {
		return nil, err
	}
	if !s.busy.TryLock() {
		return nil, ErrBusy
	}
	defer s.busy.Unlock()
	var (
		out any
		err error
	)
	switch action {
	case ActionFetch:
		var in struct{ Ref, Source string }
		if err = decode(input, map[string]*string{"ref": &in.Ref, "source": &in.Source}); err == nil {
			out, err = s.fetch(ctx, in.Ref, in.Source)
		}
	case ActionNode:
		var in struct{ Commit, Version, BuildID string }
		if err = decode(input, map[string]*string{"commit": &in.Commit, "version": &in.Version, "build_id": &in.BuildID}); err == nil {
			out, err = s.nodeRelease(ctx, in.Commit, in.Version, in.BuildID)
		}
	case ActionHubImage:
		var in struct{ Commit, BuildID string }
		if err = decode(input, map[string]*string{"commit": &in.Commit, "build_id": &in.BuildID}); err == nil {
			out, err = s.hubImage(ctx, in.Commit, in.BuildID)
		}
	case ActionNodeImage:
		var in struct{ Commit, Version, BuildID string }
		if err = decode(input, map[string]*string{"commit": &in.Commit, "version": &in.Version, "build_id": &in.BuildID}); err == nil {
			out, err = s.nodeImage(ctx, in.Commit, in.Version, in.BuildID)
		}
	case ActionSmokeTest:
		var in struct{ Image string }
		if err = decode(input, map[string]*string{"image": &in.Image}); err == nil {
			out, err = s.smoke(ctx, in.Image)
		}
	default:
		return nil, fmt.Errorf("unknown action %q", action)
	}
	if err != nil {
		return nil, err
	}
	return json.Marshal(out)
}

// Validate checks the input of an action the way the action itself will, without running
// anything. The node calls it before a call crosses the bus, so a bad value is refused twice.
func Validate(action string, input json.RawMessage) error {
	check := func(fields map[string]*string, rules map[string]*regexp.Regexp) error {
		if err := decode(input, fields); err != nil {
			return err
		}
		for k, re := range rules {
			if !re.MatchString(*fields[k]) {
				return fmt.Errorf("%s is not valid", k)
			}
		}
		return nil
	}
	var a, b, c string
	switch action {
	case ActionFetch:
		if err := check(map[string]*string{"ref": &a, "source": &b}, map[string]*regexp.Regexp{"ref": refRe, "source": sourceRe}); err != nil {
			return err
		}
		if strings.Contains(a, "..") {
			return errors.New("ref is not valid")
		}
	case ActionNode, ActionNodeImage:
		return check(map[string]*string{"commit": &a, "version": &b, "build_id": &c},
			map[string]*regexp.Regexp{"commit": commitRe, "version": versionRe, "build_id": buildIDRe})
	case ActionHubImage:
		return check(map[string]*string{"commit": &a, "build_id": &c},
			map[string]*regexp.Regexp{"commit": commitRe, "build_id": buildIDRe})
	case ActionSmokeTest:
		if err := check(map[string]*string{"image": &a}, map[string]*regexp.Regexp{"image": imageRe}); err != nil {
			return err
		}
	default:
		return fmt.Errorf("unknown action %q", action)
	}
	return nil
}

// decode reads an object whose keys are exactly the ones in fields, all strings, all present.
func decode(input json.RawMessage, fields map[string]*string) error {
	var raw map[string]json.RawMessage
	dec := json.NewDecoder(bytes.NewReader(input))
	if err := dec.Decode(&raw); err != nil {
		return fmt.Errorf("the input is not an object: %w", err)
	}
	for k, dst := range fields {
		v, ok := raw[k]
		if !ok {
			return fmt.Errorf("the input needs %s", k)
		}
		if err := json.Unmarshal(v, dst); err != nil {
			return fmt.Errorf("%s must be a string", k)
		}
		delete(raw, k)
	}
	for k := range raw {
		return fmt.Errorf("the input has an unknown field %q", k)
	}
	return nil
}

type logger struct{ b strings.Builder }

func (l *logger) add(step, out string) {
	fmt.Fprintf(&l.b, "$ %s\n%s\n", step, strings.TrimRight(out, "\n"))
}

func (l *logger) text() string {
	s := Mask(l.b.String())
	if len(s) > maxLog {
		s = s[len(s)-maxLog:]
	}
	return s
}

// gitEnv keeps git from asking, from using a global setup and from running hooks of the code.
func gitEnv() []string {
	return []string{"GIT_TERMINAL_PROMPT=0", "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_NOSYSTEM=1", "HOME=" + os.TempDir()}
}

func (s *Service) mirror(source string) (string, string, error) {
	url, ok := s.cfg.Sources[source]
	if !ok || url == "" {
		return "", "", fmt.Errorf("the source %s is not configured on this node", source)
	}
	return filepath.Join(s.cfg.Work, "mirror-"+source), url, nil
}

func (s *Service) fetch(ctx context.Context, ref, source string) (any, error) {
	if !refRe.MatchString(ref) || strings.Contains(ref, "..") || !sourceRe.MatchString(source) {
		return nil, errors.New("that is not a commit, branch or tag of a known source")
	}
	dir, url, err := s.mirror(source)
	if err != nil {
		return nil, err
	}
	l := &logger{}
	if _, err := os.Stat(filepath.Join(dir, "HEAD")); err != nil {
		out, err := s.cfg.Runner.Run(ctx, s.cfg.Work, gitEnv(), "git", "clone", "--mirror", "--", url, dir)
		l.add("git clone --mirror", out)
		if err != nil {
			return nil, fmt.Errorf("clone: %w", err)
		}
	} else {
		out, err := s.cfg.Runner.Run(ctx, dir, gitEnv(), "git", "remote", "update", "--prune")
		l.add("git remote update", out)
		if err != nil {
			return nil, fmt.Errorf("fetch: %w", err)
		}
	}
	commitOut, err := s.cfg.Runner.Run(ctx, dir, gitEnv(), "git", "rev-parse", "--verify", "--end-of-options", ref+"^{commit}")
	if err != nil {
		return nil, fmt.Errorf("%s is not a commit of the source: %w", ref, err)
	}
	commit := strings.TrimSpace(commitOut)
	if !commitRe.MatchString(commit) {
		return nil, errors.New("git did not name a commit")
	}
	subject, _ := s.cfg.Runner.Run(ctx, dir, gitEnv(), "git", "log", "-1", "--format=%s", commit)
	changes := s.changes(ctx, dir, commit)
	l.add("git log", changes)
	return map[string]any{
		"commit": commit, "subject": strings.TrimSpace(subject), "changes": changes, "log": l.text(),
	}, nil
}

// changes lists the subjects since the nearest tag (the last release), else the last twenty.
func (s *Service) changes(ctx context.Context, dir, commit string) string {
	tag, err := s.cfg.Runner.Run(ctx, dir, gitEnv(), "git", "describe", "--tags", "--abbrev=0", commit)
	args := []string{"log", "--format=%h %s", "-n", "100"}
	if t := strings.TrimSpace(tag); err == nil && refRe.MatchString(t) {
		args = append(args, t+".."+commit)
	} else {
		args = []string{"log", "--format=%h %s", "-n", "20", commit}
	}
	out, _ := s.cfg.Runner.Run(ctx, dir, gitEnv(), "git", args...)
	out = Mask(strings.TrimSpace(out))
	if len(out) > maxChanges {
		out = out[:maxChanges]
	}
	return out
}

// checkout makes a fresh clone of the source at exactly the commit; never a workshop folder.
func (s *Service) checkout(ctx context.Context, l *logger, commit, buildID string) (string, error) {
	if !commitRe.MatchString(commit) || !buildIDRe.MatchString(buildID) {
		return "", errors.New("commit or build id is not valid")
	}
	root := filepath.Join(s.cfg.Work, "builds", buildID)
	if err := os.RemoveAll(root); err != nil {
		return "", err
	}
	src := filepath.Join(root, "src")
	if err := os.MkdirAll(root, 0o750); err != nil {
		return "", err
	}
	var from string
	for _, name := range sortedKeys(s.cfg.Sources) {
		dir, _, _ := s.mirror(name)
		if _, err := s.cfg.Runner.Run(ctx, dir, gitEnv(), "git", "cat-file", "-e", commit+"^{commit}"); err == nil {
			from = dir
			break
		}
	}
	if from == "" {
		return "", errors.New("no fetched source has that commit: run build.fetch first")
	}
	out, err := s.cfg.Runner.Run(ctx, root, gitEnv(), "git", "clone", "--no-checkout", "--", from, src)
	l.add("git clone", out)
	if err != nil {
		return "", fmt.Errorf("clone: %w", err)
	}
	out, err = s.cfg.Runner.Run(ctx, src, gitEnv(), "git", "-c", "advice.detachedHead=false", "checkout", "--detach", commit)
	l.add("git checkout", out)
	if err != nil {
		return "", fmt.Errorf("checkout: %w", err)
	}
	return src, nil
}

func (s *Service) nodeRelease(ctx context.Context, commit, version, buildID string) (any, error) {
	if !versionRe.MatchString(version) {
		return nil, errors.New("version must look like 0.3.0")
	}
	l := &logger{}
	src, err := s.checkout(ctx, l, commit, buildID)
	if err != nil {
		return nil, err
	}
	out := filepath.Join(filepath.Dir(src), "out")
	env := append(os.Environ(), "GOFLAGS=-mod=readonly", "TOPCOAT_UPDATE_KEYS="+s.cfg.UpdateKeys)
	text, err := s.cfg.Runner.Run(ctx, filepath.Join(src, "node"), env, "bash", "../scripts/build-node-release.sh", version, out)
	l.add("scripts/build-node-release.sh", text)
	if err != nil {
		return nil, fmt.Errorf("build: %w", err)
	}
	entries, err := os.ReadDir(out)
	if err != nil {
		return nil, err
	}
	var artifacts []map[string]any
	for _, e := range entries {
		name := e.Name()
		osName, arch, ok := platformOf(name)
		if !ok {
			continue // SHA256SUMS and anything else is not an artifact
		}
		path := filepath.Join(out, name)
		size, sum, err := hashFile(path)
		if err != nil {
			return nil, err
		}
		if size > maxArtifactBytes {
			return nil, fmt.Errorf("%s is larger than the hub takes", name)
		}
		if err := s.cfg.Host.Upload(ctx, Upload{BuildID: buildID, Name: name, OS: osName, Arch: arch, Path: path}); err != nil {
			return nil, fmt.Errorf("upload %s: %w", name, err)
		}
		artifacts = append(artifacts, map[string]any{"name": name, "os": osName, "arch": arch, "size": size, "sha256": sum})
	}
	if len(artifacts) == 0 {
		return nil, errors.New("the build produced no artifact")
	}
	return map[string]any{"artifacts": artifacts, "log": l.text()}, nil
}

var platRe = regexp.MustCompile(`^topcoat-node-(linux|windows)-(amd64|arm64)(\.exe)?$`)

func platformOf(name string) (string, string, bool) {
	m := platRe.FindStringSubmatch(name)
	if m == nil || !nameRe.MatchString(name) {
		return "", "", false
	}
	return m[1], m[2], true
}

func hashFile(path string) (int64, string, error) {
	f, err := os.Open(path) //nolint:gosec // below the service's own directory
	if err != nil {
		return 0, "", err
	}
	defer func() { _ = f.Close() }()
	h := sha256.New()
	n, err := io.Copy(h, f)
	return n, hex.EncodeToString(h.Sum(nil)), err
}

func (s *Service) hubImage(ctx context.Context, commit, buildID string) (any, error) {
	l := &logger{}
	src, err := s.checkout(ctx, l, commit, buildID)
	if err != nil {
		return nil, err
	}
	text, err := s.cfg.Runner.Run(ctx, src, os.Environ(), "bash", "scripts/build-hub-image.sh", "topcoat-hub")
	l.add("scripts/build-hub-image.sh", text)
	if err != nil {
		return nil, fmt.Errorf("build: %w", err)
	}
	s.lastSrc = src
	image := "topcoat-hub:local-" + commit[:7]
	return s.imageResult(ctx, l, image)
}

func (s *Service) nodeImage(ctx context.Context, commit, version, buildID string) (any, error) {
	if !versionRe.MatchString(version) {
		return nil, errors.New("version must look like 0.3.0")
	}
	l := &logger{}
	src, err := s.checkout(ctx, l, commit, buildID)
	if err != nil {
		return nil, err
	}
	image := "topcoat-node-cpu:" + version
	// UNVERIFIED on real hardware: the build arguments of deploy/node-cpu/Dockerfile
	text, err := s.cfg.Runner.Run(ctx, src, os.Environ(), "docker", "build", "-f", "deploy/node-cpu/Dockerfile",
		"--build-arg", "VERSION="+version, "-t", image, ".")
	l.add("docker build node-cpu", text)
	if err != nil {
		return nil, fmt.Errorf("build: %w", err)
	}
	return s.imageResult(ctx, l, image)
}

func (s *Service) imageResult(ctx context.Context, l *logger, image string) (any, error) {
	idOut, err := s.cfg.Runner.Run(ctx, s.cfg.Work, os.Environ(), "docker", "image", "inspect", "--format", "{{.Id}}", image)
	if err != nil {
		return nil, fmt.Errorf("inspect: %w", err)
	}
	digest := strings.TrimSpace(idOut)
	if !digestRe.MatchString(digest) {
		return nil, errors.New("docker did not report a digest")
	}
	sizeOut, _ := s.cfg.Runner.Run(ctx, s.cfg.Work, os.Environ(), "docker", "image", "inspect", "--format", "{{.Size}}", image)
	var size int64
	_, _ = fmt.Sscan(strings.TrimSpace(sizeOut), &size)
	return map[string]any{"image": image, "digest": digest, "size": size, "log": l.text()}, nil
}

func (s *Service) smoke(ctx context.Context, image string) (any, error) {
	if !imageRe.MatchString(image) || strings.HasPrefix(image, "-") {
		return nil, errors.New("that is not an image name")
	}
	if s.lastSrc == "" {
		return nil, errors.New("build the hub image first: its checkout holds the smoke test")
	}
	l := &logger{}
	// the script comes from the checkout the image was built from; the image is its only argument
	text, err := s.cfg.Runner.Run(ctx, s.lastSrc, os.Environ(), "bash", "scripts/smoke-test-hub-image.sh", image)
	l.add("scripts/smoke-test-hub-image.sh", text)
	summary := "smoke test passed"
	if err != nil {
		summary = "smoke test failed: " + firstLine(err.Error())
	}
	return map[string]any{"ok": err == nil, "summary": summary, "log": l.text()}, nil
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}

func sortedKeys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
