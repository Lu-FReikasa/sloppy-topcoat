package workshop

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/tools"
)

// Template is the command a test template runs inside the container. The command is fixed here;
// a repository chooses a template by name and can never bring its own command line (D52).
type Template struct {
	Cmd []string
}

// DefaultTemplates are the test templates a repository can pick.
var DefaultTemplates = map[string]Template{
	"make-check": {Cmd: []string{"make", "check"}},
	"npm-test":   {Cmd: []string{"sh", "-c", "npm ci --no-audit --no-fund && npm test"}},
	"go-test":    {Cmd: []string{"go", "test", "./..."}},
	"pytest":     {Cmd: []string{"pytest", "-q"}},
}

// Job is one test run in a throw-away container.
type Job struct {
	Template string
	Image    string
	Cmd      []string
	// Dir is a copy of the worktree, on the host, mounted at /work.
	Dir string
	// Network is a Docker network name; empty = no network at all.
	Network  string
	MemoryMB int
	CPUMilli int
	// Timeout is the hard upper limit of a run.
	Timeout time.Duration
	// IdleTimeout ends a run that has printed nothing new for that long; 0 = no such limit.
	IdleTimeout time.Duration
	// OnOutput, when set, is called with the end of the output whenever it changed while the run
	// goes on.
	OnOutput func(tail string)
}

// Result is what a run produced.
type Result struct {
	ExitCode int
	Output   string
	TimedOut bool
	// TimedOutIdle: the run was ended because it printed nothing new for Job.IdleTimeout.
	TimedOutIdle bool
	Duration     time.Duration
}

// Runner starts a job in a fresh container with no tokens, no hub data and limits, and removes the
// container afterwards.
type Runner interface {
	Run(ctx context.Context, j Job) (Result, error)
}

const (
	defaultTestMemoryMB = 2048
	defaultTestCPUMilli = 2000
	defaultTestSeconds  = 600
	testOutputBytes     = 24 << 10
)

func (w *Workshop) capTest() capability.Capability {
	return wcap{
		name:   "repo.test",
		desc:   "Run the repository's tests: its test template (chosen when the repository was registered) runs on a copy of the worktree in a throw-away container with time, memory and CPU limits and no tokens. Returns the exit code and the end of the output. Never changes the worktree.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo"],"properties":{` + repoProp + `}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Repo string `json:"repo"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			spec, dir, job, err := w.testSetup(ctx, in.Repo)
			if err != nil {
				return nil, err
			}
			network := job.Network
			unlock := w.lock(in.Repo)
			defer unlock()
			run := filepath.Join(w.dir, ".runs", fmt.Sprintf("%s-%d", in.Repo, w.o.Now().UnixNano()))
			defer os.RemoveAll(run)
			if err := w.copyWorktree(ctx, dir, run); err != nil {
				return nil, err
			}
			job.Dir = w.hostPath(run)
			res, err := w.o.Runner.Run(ctx, job)
			if err != nil {
				return nil, fmt.Errorf("the test run could not start: %w", err)
			}
			out, _ := tools.Mask(res.Output)
			if len(out) > testOutputBytes {
				out = "[… earlier output left out]\n" + out[len(out)-testOutputBytes:]
			}
			return map[string]any{
				"repo": in.Repo, "template": spec.Template, "exit_code": res.ExitCode,
				"passed": res.ExitCode == 0 && !res.TimedOut, "timed_out": res.TimedOut,
				"duration_ms": res.Duration.Milliseconds(), "network": network != "", "output": out,
			}, nil
		},
	}
}

// testSetup checks the repository's test template and builds the job of a run (without the copy of
// the worktree, which the caller makes and puts into Job.Dir). It returns the spec, the clone's
// directory and the job.
func (w *Workshop) testSetup(ctx context.Context, repo string) (Spec, string, Job, error) {
	spec, err := w.specFor(ctx)
	if err != nil {
		return Spec{}, "", Job{}, err
	}
	dir, err := w.clone(repo)
	if err != nil {
		return Spec{}, "", Job{}, err
	}
	tpl, ok := w.o.Templates[spec.Template]
	if spec.Template == "" || spec.Template == "none" || !ok {
		return Spec{}, "", Job{}, inputErr("this repository has no test template: set one under Repositories")
	}
	rc := w.o.RunnerConfig
	image := rc.Images[spec.Template]
	if image == "" {
		return Spec{}, "", Job{}, inputErr("this node has no image for the %s template (config.tools.workshop.runner.images)", spec.Template)
	}
	network := ""
	if rc.Network != "" && slices.Contains(rc.NetworkTemplates, spec.Template) {
		network = rc.Network
	}
	return spec, dir, Job{
		Template: spec.Template, Image: image, Cmd: tpl.Cmd, Network: network,
		MemoryMB: orDefault(rc.MemoryMB, defaultTestMemoryMB), CPUMilli: orDefault(rc.CPUMilli, defaultTestCPUMilli),
		Timeout: time.Duration(orDefault(rc.TimeoutSeconds, defaultTestSeconds)) * time.Second,
	}, nil
}

func orDefault(v, d int) int {
	if v > 0 {
		return v
	}
	return d
}

// hostPath maps a path under the workshop directory to the path the Docker daemon sees.
func (w *Workshop) hostPath(p string) string {
	if h := w.o.RunnerConfig.HostDir; h != "" {
		if rel, err := filepath.Rel(w.dir, p); err == nil {
			return filepath.Join(h, rel)
		}
	}
	return p
}

// copyWorktree copies the files of the worktree (tracked and new, without .git and without files
// that look like credentials) to dst, so a test run cannot touch the clone.
func (w *Workshop) copyWorktree(ctx context.Context, dir, dst string) error {
	out, err := w.git(ctx, dir, nil, "", "ls-files", "-z", "--cached", "--others", "--exclude-standard")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dst, 0o755); err != nil {
		return err
	}
	var total int64
	for _, rel := range strings.Split(out, "\x00") {
		if rel == "" || secretName(rel) {
			continue
		}
		src := filepath.Join(dir, filepath.FromSlash(rel))
		st, err := os.Lstat(src)
		if err != nil || !st.Mode().IsRegular() { // symbolic links are not followed
			continue
		}
		total += st.Size()
		if total > int64(w.o.MaxRepoMB)<<20 {
			return inputErr("the worktree is larger than %d MB", w.o.MaxRepoMB)
		}
		to := filepath.Join(dst, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(to), 0o755); err != nil {
			return err
		}
		if err := copyFile(src, to, st.Mode().Perm()); err != nil {
			return err
		}
	}
	return nil
}

func copyFile(src, dst string, perm os.FileMode) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, perm|0o200)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}
