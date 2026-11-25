package workshop

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
)

// slowRunner is a test run that goes on until the test lets it end.
type slowRunner struct {
	mu       sync.Mutex
	jobs     []Job
	started  chan struct{}
	release  chan Result
	canceled chan struct{}
}

func newSlowRunner() *slowRunner {
	return &slowRunner{started: make(chan struct{}, 8), release: make(chan Result, 1), canceled: make(chan struct{}, 8)}
}

func (r *slowRunner) Run(ctx context.Context, j Job) (Result, error) {
	r.mu.Lock()
	r.jobs = append(r.jobs, j)
	r.mu.Unlock()
	if j.OnOutput != nil {
		j.OnOutput("collecting ...\n")
	}
	r.started <- struct{}{}
	select {
	case res := <-r.release:
		return res, nil
	case <-ctx.Done():
		r.canceled <- struct{}{}
		return Result{}, ctx.Err()
	}
}

func jobFixture(t *testing.T, r Runner) *fixture {
	t.Helper()
	f := newFixture(t, func(o *Options) {
		o.Runner = r
		o.RunnerConfig = protocol.WorkshopRunner{
			Type: protocol.WorkshopRunnerDocker, Images: map[string]string{"make-check": "builder:1"},
			TimeoutSeconds: 900, IdleSeconds: 120,
		}
	})
	f.must(t, "repo.sync", repoIn{"repo": "demo"})
	return f
}

func TestATestRunGoesOnAfterTheCallThatStartedItAndShowsItsState(t *testing.T) {
	r := newSlowRunner()
	f := jobFixture(t, r)
	begin := time.Now()
	got := f.must(t, "repo.test_start", repoIn{"repo": "demo"})
	if time.Since(begin) > 5*time.Second {
		t.Fatal("the start waited for the run")
	}
	id, _ := got["job_id"].(string)
	if len(id) != 24 || got["state"] != "running" || got["idle_limit_seconds"].(float64) != 120 || got["hard_limit_seconds"].(float64) != 900 {
		t.Fatalf("start: %v", got)
	}
	<-r.started
	if j := r.jobs[0]; j.Timeout != 900*time.Second || j.IdleTimeout != 120*time.Second || j.Cmd[0] != "make" {
		t.Fatalf("job: %+v", j)
	}
	st := f.must(t, "repo.test_status", repoIn{"repo": "demo", "job_id": id})
	if st["state"] != "running" || st["running"] != true || st["passed"] != nil || !strings.Contains(st["log"].(string), "collecting") {
		t.Fatalf("running: %v", st)
	}
	// a second run of the same repository is refused and names the first
	_, err := f.call(t, "repo.test_start", repoIn{"repo": "demo"})
	refused(t, err, id)
	// a status call that waits returns early when the run ends
	go func() {
		time.Sleep(150 * time.Millisecond)
		r.release <- Result{ExitCode: 0, Output: "3 passed\n", Duration: time.Second}
	}()
	begin = time.Now()
	st = f.must(t, "repo.test_status", repoIn{"repo": "demo", "job_id": id, "wait_seconds": 10})
	if time.Since(begin) > 5*time.Second {
		t.Fatalf("waited for the whole time: %s", time.Since(begin))
	}
	if st["state"] != "passed" || st["passed"] != true || st["running"] != false || st["exit_code"].(float64) != 0 || !strings.Contains(st["log"].(string), "3 passed") {
		t.Fatalf("passed: %v", st)
	}
	if _, err := f.call(t, "repo.test_start", repoIn{"repo": "demo"}); err != nil {
		t.Fatalf("a new run after the first ended: %v", err)
	}
}

func TestHowABackgroundRunEnds(t *testing.T) {
	for name, tc := range map[string]struct {
		res   Result
		state string
		pass  bool
	}{
		"a failing test": {Result{ExitCode: 1, Output: "FAILED t\npassword = hunter2hunter2\n"}, "failed", false},
		"a quiet run":    {Result{ExitCode: 137, TimedOutIdle: true}, "timed_out_idle", false},
		"a run too long": {Result{ExitCode: 137, TimedOut: true}, "timed_out_cap", false},
	} {
		t.Run(name, func(t *testing.T) {
			r := newSlowRunner()
			f := jobFixture(t, r)
			id := f.must(t, "repo.test_start", repoIn{"repo": "demo"})["job_id"].(string)
			<-r.started
			r.release <- tc.res
			st := f.must(t, "repo.test_status", repoIn{"repo": "demo", "job_id": id, "wait_seconds": 10})
			if st["state"] != tc.state || st["passed"] != tc.pass || st["running"] != false {
				t.Fatalf("%s: %v", name, st)
			}
			if tc.state != "failed" && st["timed_out"] != true {
				t.Fatalf("timed_out: %v", st)
			}
			if strings.Contains(st["log"].(string), "hunter2") {
				t.Fatalf("output not masked: %v", st["log"])
			}
		})
	}
}

func TestACanceledRunStopsAndStaysCanceled(t *testing.T) {
	r := newSlowRunner()
	f := jobFixture(t, r)
	id := f.must(t, "repo.test_start", repoIn{"repo": "demo"})["job_id"].(string)
	<-r.started
	st := f.must(t, "repo.test_cancel", repoIn{"repo": "demo", "job_id": id})
	if st["state"] != "canceled" || st["running"] != false {
		t.Fatalf("cancel: %v", st)
	}
	select {
	case <-r.canceled:
	case <-time.After(5 * time.Second):
		t.Fatal("the runner was not told to stop")
	}
	if st = f.must(t, "repo.test_status", repoIn{"repo": "demo", "job_id": id}); st["state"] != "canceled" {
		t.Fatalf("status: %v", st)
	}
}

func TestOnlyTheOwnRepositoryAndAKnownJobAreShown(t *testing.T) {
	r := newSlowRunner()
	f := jobFixture(t, r)
	id := f.must(t, "repo.test_start", repoIn{"repo": "demo"})["job_id"].(string)
	<-r.started
	_, err := f.call(t, "repo.test_status", repoIn{"repo": "other", "job_id": id})
	refused(t, err, "no test run")
	_, err = f.call(t, "repo.test_status", repoIn{"repo": "demo", "job_id": strings.Repeat("0", 24)})
	refused(t, err, "no test run")
	_, err = f.call(t, "repo.test_status", repoIn{"repo": "demo", "job_id": "x"})
	if err == nil {
		t.Fatal("a job id that is not 24 hex digits was looked up")
	}
	r.release <- Result{}
}

func TestTheRunnerErrorEndsTheRunAsAnError(t *testing.T) {
	f := jobFixture(t, errRunner{})
	id := f.must(t, "repo.test_start", repoIn{"repo": "demo"})["job_id"].(string)
	st := f.must(t, "repo.test_status", repoIn{"repo": "demo", "job_id": id, "wait_seconds": 10})
	if st["state"] != "error" || !strings.Contains(st["error"].(string), "boom") {
		t.Fatalf("status: %v", st)
	}
}

type errRunner struct{}

func (errRunner) Run(context.Context, Job) (Result, error) { return Result{}, errBoom }

var errBoom = &boomError{}

type boomError struct{}

func (*boomError) Error() string { return "boom" }

func TestOneTestFileIsAnArgumentNeverAnOption(t *testing.T) {
	job := Job{Template: "pytest", Cmd: []string{"pytest", "-q"}}
	cmd, err := withTestPath(job, "tests/_probe_filter.py")
	if err != nil || strings.Join(cmd, " ") != "pytest -q tests/_probe_filter.py" {
		t.Fatalf("one file: %v %v", cmd, err)
	}
	if strings.Join(job.Cmd, " ") != "pytest -q" {
		t.Fatalf("the template's command was changed: %v", job.Cmd)
	}
	if cmd, err := withTestPath(job, ""); err != nil || len(cmd) != 2 {
		t.Fatalf("no path: %v %v", cmd, err)
	}
	for _, bad := range []string{"-k x.py", "--co.py", "/etc/x.py", "../x.py", "tests/../../x.py", "tests/x.txt", "a b.py", "./x.py"} {
		if _, err := withTestPath(job, bad); err == nil {
			t.Fatalf("%q was taken", bad)
		}
	}
	if _, err := withTestPath(Job{Template: "go-test", Cmd: []string{"go", "test"}}, "x.py"); err == nil {
		t.Fatal("a path for another template was taken")
	}
}
