package workshop

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/tools"
)

// Background test runs (repo.test_start, repo.test_status, repo.test_cancel): a run does not
// belong to the call that started it. The start returns a job id at once; the run goes on in the
// node, and the caller asks for its state, optionally waiting a few seconds for news. A run ends
// when the test ends, when it printed nothing new for the idle limit, when it hit the hard limit
// or when someone cancels it - never because a call to ask for it ran out of time.
const (
	defaultRunSeconds  = 1800
	defaultIdleSeconds = 180
	maxStatusWait      = 15 // seconds a status call may wait for news (the call itself has 20 s)
	maxRunsPerNode     = 2
	keepFinished       = time.Hour
	keepMax            = 20
	cancelWait         = 10 * time.Second
)

// Run states.
const (
	runRunning   = "running"
	runPassed    = "passed"
	runFailed    = "failed"
	runIdle      = "timed_out_idle"
	runCap       = "timed_out_cap"
	runCanceled  = "canceled"
	runError     = "error"
	runLostState = "lost" // not kept by a node: only the hub ever says it, after a node restart
)

type testRun struct {
	id, repo, template  string
	started             time.Time
	idleLimit, capLimit time.Duration
	cancel              context.CancelFunc
	done                chan struct{}

	mu         sync.Mutex
	state      string
	tail       string
	lastChange time.Time
	exitCode   int
	finished   time.Time
	errMsg     string
}

func (r *testRun) setTail(tail string, now time.Time) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if tail != r.tail {
		r.tail, r.lastChange = tail, now
	}
}

func newRunID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// gcRuns forgets finished runs that are old or too many. Caller holds w.runsMu.
func (w *Workshop) gcRuns(now time.Time) {
	var done []*testRun
	for id, r := range w.runs {
		r.mu.Lock()
		fin, at := r.state != runRunning, r.finished
		r.mu.Unlock()
		if !fin {
			continue
		}
		if now.Sub(at) > keepFinished {
			delete(w.runs, id)
			continue
		}
		done = append(done, r)
	}
	if len(done) > keepMax {
		sort.Slice(done, func(i, j int) bool { return done[i].finished.Before(done[j].finished) })
		for _, r := range done[:len(done)-keepMax] {
			delete(w.runs, r.id)
		}
	}
}

func (w *Workshop) capTestStart() capability.Capability {
	return wcap{
		name:   "repo.test_start",
		desc:   "Start the repository's tests in the background and return at once with a job_id. The run goes on by itself (a long test suite is fine): check it with repo.test_status, stop it with repo.test_cancel. The run is ended only when it prints nothing new for a long time or runs far too long, never because you waited. Do other work meanwhile. Same test template, copy and limits as repo.test; never changes the worktree. path (pytest template only): run just this one test file.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo"],"properties":{` + repoProp + `,` + testPathProp + `}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Repo string `json:"repo"`
				Path string `json:"path"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			_, dir, job, err := w.testSetup(ctx, in.Repo)
			if err != nil {
				return nil, err
			}
			if job.Cmd, err = withTestPath(job, in.Path); err != nil {
				return nil, err
			}
			now := w.o.Now()
			w.runsMu.Lock()
			w.gcRuns(now)
			running := 0
			for _, r := range w.runs {
				r.mu.Lock()
				live := r.state == runRunning
				r.mu.Unlock()
				if !live {
					continue
				}
				running++
				if r.repo == in.Repo {
					w.runsMu.Unlock()
					return nil, inputErr("a test run of %q is already going on: job_id %s (repo.test_status shows it, repo.test_cancel stops it)", in.Repo, r.id)
				}
			}
			if running >= maxRunsPerNode {
				w.runsMu.Unlock()
				return nil, inputErr("this node already runs %d test runs: wait for one to end (repo.test_status) or cancel one", running)
			}
			rc := w.o.RunnerConfig
			run := &testRun{
				id: newRunID(), repo: in.Repo, template: job.Template, started: now, state: runRunning, lastChange: now,
				idleLimit: time.Duration(orDefault(rc.IdleSeconds, defaultIdleSeconds)) * time.Second,
				capLimit:  time.Duration(orDefault(rc.TimeoutSeconds, defaultRunSeconds)) * time.Second,
				done:      make(chan struct{}),
			}
			w.runs[run.id] = run
			w.runsMu.Unlock()

			copyDir := filepath.Join(w.dir, ".runs", fmt.Sprintf("%s-%d", in.Repo, now.UnixNano()))
			unlock := w.lock(in.Repo)
			err = w.copyWorktree(ctx, dir, copyDir)
			unlock()
			if err != nil {
				_ = os.RemoveAll(copyDir)
				w.finishRun(run, Result{}, err)
				return nil, err
			}
			job.Dir = w.hostPath(copyDir)
			job.Timeout, job.IdleTimeout = run.capLimit, run.idleLimit
			job.OnOutput = func(tail string) { run.setTail(tail, w.o.Now()) }
			rctx, cancel := context.WithCancel(w.bg)
			run.cancel = cancel
			go func() {
				defer os.RemoveAll(copyDir)
				defer cancel()
				res, err := w.o.Runner.Run(rctx, job)
				w.finishRun(run, res, err)
			}()
			return map[string]any{
				"repo": in.Repo, "job_id": run.id, "state": runRunning, "template": job.Template,
				"idle_limit_seconds": int(run.idleLimit / time.Second), "hard_limit_seconds": int(run.capLimit / time.Second),
			}, nil
		},
	}
}

// finishRun records how a run ended.
func (w *Workshop) finishRun(r *testRun, res Result, err error) {
	out, _ := tools.Mask(res.Output)
	r.mu.Lock()
	switch {
	case r.state == runCanceled: // someone asked for the end: it stays canceled whatever the runner says
	case err != nil:
		r.state, r.errMsg = runError, "the test run could not run: "+err.Error()
	case res.TimedOutIdle:
		r.state, r.exitCode = runIdle, res.ExitCode
	case res.TimedOut:
		r.state, r.exitCode = runCap, res.ExitCode
	case res.ExitCode == 0:
		r.state = runPassed
	default:
		r.state, r.exitCode = runFailed, res.ExitCode
	}
	if r.state != runCanceled || res.Output != "" {
		if out != "" {
			r.tail = out
		}
	}
	r.finished = w.o.Now()
	r.mu.Unlock()
	close(r.done)
}

func (w *Workshop) findRun(repo, id string) (*testRun, error) {
	w.runsMu.Lock()
	defer w.runsMu.Unlock()
	r, ok := w.runs[id]
	if !ok || r.repo != repo {
		return nil, inputErr("no test run %q of repository %q on this node (it may have ended long ago or the node was restarted: start a new one)", id, repo)
	}
	return r, nil
}

// view is the state of a run as the caller sees it.
func (w *Workshop) viewRun(r *testRun) map[string]any {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := w.o.Now()
	end := now
	if r.state != runRunning {
		end = r.finished
	}
	tail := r.tail
	if len(tail) > testOutputBytes {
		tail = "[… earlier output left out]\n" + tail[len(tail)-testOutputBytes:]
	}
	tail, _ = tools.Mask(tail)
	m := map[string]any{
		"repo": r.repo, "job_id": r.id, "template": r.template, "state": r.state,
		"running": r.state == runRunning, "elapsed_ms": end.Sub(r.started).Milliseconds(),
		"log": tail, "idle_limit_seconds": int(r.idleLimit / time.Second), "hard_limit_seconds": int(r.capLimit / time.Second),
	}
	if r.state == runRunning {
		m["quiet_for_ms"] = now.Sub(r.lastChange).Milliseconds()
		return m
	}
	m["passed"] = r.state == runPassed
	m["exit_code"] = r.exitCode
	m["timed_out"] = r.state == runIdle || r.state == runCap
	if r.errMsg != "" {
		m["error"] = r.errMsg
	}
	return m
}

func (w *Workshop) capTestStatus() capability.Capability {
	return wcap{
		name: "repo.test_status", readOnly: true,
		desc: "The state of a background test run (from repo.test_start): running, passed, failed, timed_out_idle, timed_out_cap, canceled or error, how long it has run, and the end of its output. With wait_seconds (1-15) the call waits for the run to end or for 15 s at most, so you need not ask again and again. Only a state of passed or failed is a test result; while it is running there is none.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo","job_id"],"properties":{` + repoProp +
			`,"job_id":{"type":"string","pattern":"^[0-9a-f]{24}$","description":"the job_id repo.test_start returned"}` +
			`,"wait_seconds":{"type":"integer","minimum":0,"maximum":15,"description":"wait this long for the run to end"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var repo, id string
			var wait int
			if err := decodeJob(raw, &repo, &id, &wait); err != nil {
				return nil, err
			}
			r, err := w.findRun(repo, id)
			if err != nil {
				return nil, err
			}
			if wait > 0 {
				t := time.NewTimer(time.Duration(min(wait, maxStatusWait)) * time.Second)
				defer t.Stop()
				select {
				case <-r.done:
				case <-t.C:
				case <-ctx.Done():
				}
			}
			return w.viewRun(r), nil
		},
	}
}

func (w *Workshop) capTestCancel() capability.Capability {
	return wcap{
		name: "repo.test_cancel",
		desc: "Stop a background test run (from repo.test_start). The container is removed. Changes nothing in the worktree.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo","job_id"],"properties":{` + repoProp +
			`,"job_id":{"type":"string","pattern":"^[0-9a-f]{24}$"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var repo, id string
			var wait int
			if err := decodeJob(raw, &repo, &id, &wait); err != nil {
				return nil, err
			}
			r, err := w.findRun(repo, id)
			if err != nil {
				return nil, err
			}
			r.mu.Lock()
			if r.state == runRunning {
				r.state = runCanceled
			}
			r.mu.Unlock()
			if r.cancel != nil {
				r.cancel()
			}
			select {
			case <-r.done:
			case <-time.After(cancelWait):
			case <-ctx.Done():
			}
			return w.viewRun(r), nil
		},
	}
}

func decodeJob(raw json.RawMessage, repo, id *string, wait *int) error {
	var in struct {
		Repo  string `json:"repo"`
		JobID string `json:"job_id"`
		Wait  int    `json:"wait_seconds"`
	}
	if err := decode(raw, &in); err != nil {
		return err
	}
	*repo, *id, *wait = in.Repo, in.JobID, in.Wait
	return nil
}

// testPathProp is the optional one test file of repo.test_start (2026-11-19: an issue's
// investigation tries a guess with one small test instead of the whole suite).
const testPathProp = `"path":{"type":"string","maxLength":200,"pattern":"^[A-Za-z0-9_][A-Za-z0-9_./-]*\\.py$","description":"one test file, relative to the test root (pytest template only)"}`

// withTestPath is the template's command with one test file added. Only the pytest template takes
// one, and the file is a plain relative .py path: never an option (no leading "-"), never outside
// the copy (no "..", not absolute). Without a path the command is the template's own.
func withTestPath(job Job, path string) ([]string, error) {
	if path == "" {
		return job.Cmd, nil
	}
	if job.Template != "pytest" {
		return nil, inputErr("path is only for the pytest template, not %q", job.Template)
	}
	clean := filepath.ToSlash(filepath.Clean(path))
	if !testPathRe.MatchString(path) || clean != path || strings.Contains(path, "..") {
		return nil, inputErr("path must be one relative .py file without .. (got %q)", path)
	}
	cmd := append(append([]string{}, job.Cmd...), path)
	return cmd, nil
}

var testPathRe = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_./-]{0,198}\.py$`)
