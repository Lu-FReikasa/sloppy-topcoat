package workshop

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/tools"
)

func needGit(t *testing.T) {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git is not installed")
	}
}

func run(t *testing.T, dir string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_NOSYSTEM=1",
		"GIT_AUTHOR_NAME=T", "GIT_AUTHOR_EMAIL=t@example.invalid", "GIT_COMMITTER_NAME=T", "GIT_COMMITTER_EMAIL=t@example.invalid")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
}

func write(t *testing.T, dir, rel, text string) {
	t.Helper()
	p := filepath.Join(dir, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}

// origin builds a small repository that stands in for the remote.
func origin(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	run(t, dir, "init", "-q", "-b", "main")
	write(t, dir, "app/main.py", "def greet(name):\n    return 'Hello ' + name\n\n\ndef twice(x):\n    return x * 2\n")
	write(t, dir, "app/util.py", "def twice(x):\n    return x * 2\n")
	write(t, dir, "README.md", "# demo\n")
	write(t, dir, ".env", "API_KEY=sk-live-0123456789abcdef0123456789\n")
	write(t, dir, ".env.example", "API_KEY=\n")
	write(t, dir, "deploy/server.key", "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n")
	write(t, dir, "config.py", "PASSWORD = 'hunter2hunter2'\nNAME = 'x'\n")
	write(t, dir, ".github/workflows/ci.yml", "on: push\n")
	// Windows needs a privilege for symbolic links: without one there is no link to refuse
	if err := os.Symlink("/etc/hostname", filepath.Join(dir, "link")); err != nil {
		if runtime.GOOS != "windows" {
			t.Fatal(err)
		}
	} else {
		linkMade = true
	}
	run(t, dir, "add", "-A")
	run(t, dir, "commit", "-q", "-m", "first")
	return dir
}

type fixture struct {
	w    *Workshop
	src  string
	spec Spec
	ctx  context.Context
}

func newFixture(t *testing.T, mod func(*Options)) *fixture {
	t.Helper()
	needGit(t)
	src := origin(t)
	o := Options{Dir: filepath.Join(t.TempDir(), "ws"), allowFileURLs: true}
	if mod != nil {
		mod(&o)
	}
	w, err := New(o)
	if err != nil {
		t.Fatal(err)
	}
	spec := Spec{URL: fileURL(src), Branch: "main", Template: "make-check"}
	return &fixture{w: w, src: src, spec: spec, ctx: WithSpec(context.Background(), spec)}
}

func (f *fixture) call(t *testing.T, name string, in any) (map[string]any, error) {
	t.Helper()
	var c capability.Capability
	for _, x := range f.w.Registry() {
		if x.Name() == name {
			c = x
		}
	}
	if c == nil {
		t.Fatalf("no capability %s", name)
	}
	raw, _ := json.Marshal(in)
	out, err := c.Invoke(f.ctx, raw)
	if err != nil {
		return nil, err
	}
	var m map[string]any
	if err := json.Unmarshal(out, &m); err != nil {
		t.Fatal(err)
	}
	return m, nil
}

func (f *fixture) must(t *testing.T, name string, in any) map[string]any {
	t.Helper()
	m, err := f.call(t, name, in)
	if err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	return m
}

func refused(t *testing.T, err error, want string) {
	t.Helper()
	var ie *tools.InputError
	if !errors.As(err, &ie) {
		t.Fatalf("want an input error containing %q, got %v", want, err)
	}
	if !strings.Contains(ie.Msg, want) {
		t.Fatalf("want %q in %q", want, ie.Msg)
	}
}

type repoIn map[string]any

// linkMade tells the tests whether the fixture could create its symbolic link.
var linkMade bool

// fileURL is the file:// URL of a local path: on Windows a path starts with a drive letter, which
// needs a third slash (file:///C:/dir), otherwise the URL does not parse.
func fileURL(path string) string {
	p := filepath.ToSlash(path)
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	return "file://" + p
}

func TestSyncClonesAndReportsTheState(t *testing.T) {
	f := newFixture(t, nil)
	got := f.must(t, "repo.sync", repoIn{"repo": "demo"})
	if got["branch"] != "main" || got["clean"] != true || got["head"] == "" {
		t.Fatalf("sync: %v", got)
	}
	st := f.must(t, "repo.status", repoIn{"repo": "demo"})
	if st["clean"] != true || st["last_commit"] != "first" {
		t.Fatalf("status: %v", st)
	}
	// a repository that is not cloned is refused with the way forward
	_, err := f.call(t, "repo.status", repoIn{"repo": "other"})
	refused(t, err, "run repo.sync first")
}

func TestSyncFastForwardsOnlyACleanDefaultBranch(t *testing.T) {
	f := newFixture(t, nil)
	f.must(t, "repo.sync", repoIn{"repo": "demo"})
	write(t, f.src, "NEW.md", "new\n")
	run(t, f.src, "add", "-A")
	run(t, f.src, "commit", "-q", "-m", "second")
	got := f.must(t, "repo.sync", repoIn{"repo": "demo"})
	if got["updated"] != true || got["behind_default"].(float64) != 0 {
		t.Fatalf("clean: %v", got)
	}
	if _, err := os.Stat(filepath.Join(f.w.dir, "demo", "NEW.md")); err != nil {
		t.Fatal("the new file did not arrive")
	}
	// with a change in the worktree the sync fetches but leaves the files alone
	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "mine.txt", "content": "x"})
	write(t, f.src, "THIRD.md", "3\n")
	run(t, f.src, "add", "-A")
	run(t, f.src, "commit", "-q", "-m", "third")
	got = f.must(t, "repo.sync", repoIn{"repo": "demo"})
	if got["updated"] != false || got["behind_default"].(float64) != 1 || got["clean"] != false {
		t.Fatalf("dirty: %v", got)
	}
}

func TestReadListAndSearchHideSecrets(t *testing.T) {
	f := newFixture(t, nil)
	f.must(t, "repo.sync", repoIn{"repo": "demo"})

	list := f.must(t, "repo.list", repoIn{"repo": "demo"})
	files := strings.Join(toStrings(list["files"]), " ")
	for _, bad := range []string{".env ", "server.key"} {
		if strings.Contains(files+" ", bad) {
			t.Fatalf("%s listed: %s", bad, files)
		}
	}
	for _, good := range []string{"app/main.py", ".env.example", "README.md"} {
		if !strings.Contains(files, good) {
			t.Fatalf("%s missing: %s", good, files)
		}
	}

	read := f.must(t, "repo.read", repoIn{"repo": "demo", "path": "config.py"})
	if strings.Contains(read["text"].(string), "hunter2") || read["masked"].(float64) < 1 {
		t.Fatalf("password not masked: %v", read)
	}
	part := f.must(t, "repo.read", repoIn{"repo": "demo", "path": "app/main.py", "start_line": 5, "end_line": 6})
	if part["text"] != "def twice(x):\n    return x * 2" || part["start_line"].(float64) != 5 {
		t.Fatalf("range: %v", part)
	}

	for path, why := range map[string]string{
		".env": "credentials", "deploy/server.key": "credentials", ".git/config": "not available",
		"../outside": "inside the repository", "/etc/passwd": "relative", "link": "symbolic link", "nope.txt": "not a file",
	} {
		if path == "link" && !linkMade {
			continue
		}
		_, err := f.call(t, "repo.read", repoIn{"repo": "demo", "path": path})
		if err == nil {
			t.Fatalf("%s was readable", path)
		}
		refused(t, err, why)
	}

	hits := f.must(t, "repo.search", repoIn{"repo": "demo", "query": "twice"})
	text := hits["text"].(string)
	if !strings.Contains(text, "app/main.py:5:") || !strings.Contains(text, "app/util.py:1:") {
		t.Fatalf("search: %v", hits)
	}
	if none := f.must(t, "repo.search", repoIn{"repo": "demo", "query": "no-such-text"}); none["matches"].(float64) != 0 {
		t.Fatalf("no match is not an error: %v", none)
	}
	secret := f.must(t, "repo.search", repoIn{"repo": "demo", "query": "sk-live"})
	if secret["matches"].(float64) != 0 {
		t.Fatalf("the .env file was searched: %v", secret)
	}
}

func toStrings(v any) []string {
	var out []string
	for _, x := range v.([]any) {
		out = append(out, x.(string))
	}
	return out
}

func TestEditWriteAndDiff(t *testing.T) {
	f := newFixture(t, nil)
	f.must(t, "repo.sync", repoIn{"repo": "demo"})

	_, err := f.call(t, "repo.edit", repoIn{"repo": "demo", "path": "app/main.py", "old": "x * 2", "new": "x * 3"})
	_ = err // "x * 2" occurs once in main.py
	if err != nil {
		t.Fatal(err)
	}
	_, err = f.call(t, "repo.edit", repoIn{"repo": "demo", "path": "app/main.py", "old": "nothing like this", "new": "x"})
	refused(t, err, "does not occur")
	_, err = f.call(t, "repo.edit", repoIn{"repo": "demo", "path": "app/main.py", "old": "return", "new": "yield"})
	refused(t, err, "occurs 2 times")

	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "docs/new/NOTES.md", "content": "hello\n"})
	diff := f.must(t, "repo.diff", repoIn{"repo": "demo"})
	text := diff["diff"].(string)
	if !strings.Contains(text, "-    return x * 2") || !strings.Contains(text, "+    return x * 3") || !strings.Contains(text, "docs/new/NOTES.md") {
		t.Fatalf("diff: %s", text)
	}
	if got := toStrings(diff["files"]); len(got) != 2 {
		t.Fatalf("files: %v", got)
	}
	if stat := f.must(t, "repo.diff", repoIn{"repo": "demo", "stat": true}); !strings.Contains(stat["diff"].(string), "2 files changed") {
		t.Fatalf("stat: %v", stat)
	}

	for _, bad := range []repoIn{
		{"repo": "demo", "path": ".github/workflows/ci.yml", "content": "x"},
		{"repo": "demo", "path": ".git/hooks/pre-commit", "content": "x"},
		{"repo": "demo", "path": ".env", "content": "x"},
		{"repo": "demo", "path": "link", "content": "x"},
		{"repo": "demo", "path": "../escape.txt", "content": "x"},
		{"repo": "demo", "path": "app/bin.dat", "content": "a\x00b"},
	} {
		if bad["path"] == "link" && !linkMade {
			continue
		}
		if _, err := f.call(t, "repo.write", bad); err == nil {
			t.Fatalf("write %v was accepted", bad["path"])
		}
	}
	if _, err := os.Stat(filepath.Join(f.w.dir, "escape.txt")); err == nil {
		t.Fatal("a file was written outside the clone")
	}
}

func TestApplyPatch(t *testing.T) {
	f := newFixture(t, nil)
	f.must(t, "repo.sync", repoIn{"repo": "demo"})
	good := "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n # demo\n+more text\n"
	f.must(t, "repo.apply_patch", repoIn{"repo": "demo", "patch": good})
	if b, _ := os.ReadFile(filepath.Join(f.w.dir, "demo", "README.md")); string(b) != "# demo\nmore text\n" {
		t.Fatalf("README: %q", b)
	}
	// all or nothing: a patch that does not apply changes nothing
	bad := "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n # something else\n+x\n"
	_, err := f.call(t, "repo.apply_patch", repoIn{"repo": "demo", "patch": bad})
	refused(t, err, "does not apply")
	// a pipeline file and a path outside are refused before anything is applied
	wf := "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1,2 @@\n on: push\n+x: y\n"
	_, err = f.call(t, "repo.apply_patch", repoIn{"repo": "demo", "patch": wf})
	refused(t, err, "pipeline definition")
	out := "diff --git a/../x b/../x\nnew file mode 100644\n--- /dev/null\n+++ b/../x\n@@ -0,0 +1 @@\n+x\n"
	if _, err = f.call(t, "repo.apply_patch", repoIn{"repo": "demo", "patch": out}); err == nil {
		t.Fatal("a patch outside the repository was accepted")
	}
}

func TestStartAndReset(t *testing.T) {
	f := newFixture(t, nil)
	f.must(t, "repo.sync", repoIn{"repo": "demo"})
	got := f.must(t, "repo.start", repoIn{"repo": "demo", "topic": "Fix the Paging!"})
	if got["branch"] != "topcoat/fix-the-paging" {
		t.Fatalf("branch: %v", got)
	}
	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "a.txt", "content": "x"})
	_, err := f.call(t, "repo.start", repoIn{"repo": "demo", "topic": "another"})
	refused(t, err, "has changes")
	f.must(t, "repo.start", repoIn{"repo": "demo", "topic": "another", "discard": true})
	if st := f.must(t, "repo.status", repoIn{"repo": "demo"}); st["clean"] != true || st["branch"] != "topcoat/another" {
		t.Fatalf("status: %v", st)
	}
	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "b.txt", "content": "x"})
	f.must(t, "repo.reset", repoIn{"repo": "demo"})
	if st := f.must(t, "repo.status", repoIn{"repo": "demo"}); st["clean"] != true {
		t.Fatalf("reset: %v", st)
	}
}

func TestTheRepositoryIsChecked(t *testing.T) {
	needGit(t)
	w, err := New(Options{Dir: filepath.Join(t.TempDir(), "ws")}) // no test shortcuts
	if err != nil {
		t.Fatal(err)
	}
	for url, why := range map[string]string{
		"http://github.com/o/r":                 "plain https URL",
		"https://user:pw@github.com/o/r":        "plain https URL",
		"https://github.com:444/o/r":            "plain https URL",
		"https://github.com/o/r?x=1":            "plain https URL",
		"https://evil.example.invalid/o/r":      "allowed hosts",
		"https://github.com/o/r/extra/segments": "look like",
		"file:///etc":                           "plain https URL",
		"":                                      "not valid",
	} {
		err := w.checkSpec(Spec{URL: url, Branch: "main"})
		if err == nil {
			t.Fatalf("%q was accepted", url)
		}
		refused(t, err, why)
	}
	if err := w.checkSpec(Spec{URL: "https://github.com/owner/name.git", Branch: "main"}); err != nil {
		t.Fatalf("a normal URL was refused: %v", err)
	}
	for _, b := range []string{"", "-x", "a..b", "a b", "x/", "main.lock"} {
		if err := w.checkSpec(Spec{URL: "https://github.com/o/r", Branch: b}); err == nil {
			t.Fatalf("branch %q was accepted", b)
		}
	}
	// no repository handed over: the capability says so
	f := newFixture(t, nil)
	f.ctx = context.Background()
	_, err = f.call(t, "repo.sync", repoIn{"repo": "demo"})
	refused(t, err, "did not say which repository")
	f.ctx = WithSpec(context.Background(), f.spec)
	_, err = f.call(t, "repo.sync", repoIn{"repo": "../up"})
	refused(t, err, "short name")
}

func TestTheTokenStaysOutOfMessages(t *testing.T) {
	h := authFor(Spec{URL: "https://github.com/o/r", Token: "ghp_abcdefghijklmnopqrstuvwxyz0123456789"})
	if h == nil || h.key != "http.https://github.com/.extraHeader" {
		t.Fatalf("header: %+v", h)
	}
	msg := "fatal: " + h.secrets[0] + " and ghp_abcdefghijklmnopqrstuvwxyz0123456789 were echoed"
	if got := h.scrub(msg); strings.Contains(got, "ghp_") || strings.Contains(got, h.secrets[0]) {
		t.Fatalf("not scrubbed: %s", got)
	}
	if authFor(Spec{URL: "https://github.com/o/r"}) != nil {
		t.Fatal("no token, no header")
	}
	if authFor(Spec{URL: "file:///x", Token: "t"}) != nil {
		t.Fatal("a header for a non-https URL")
	}
}

func TestWhatRunsWithoutAConfirmation(t *testing.T) {
	f := newFixture(t, func(o *Options) {
		o.Runner = &fakeRunner{}
		o.RunnerConfig.Images = map[string]string{"build-node": "golang:1.27"}
	})
	readOnly := map[string]bool{"repo.status": true, "repo.list": true, "repo.read": true, "repo.search": true, "repo.diff": true, "repo.build_list": true, "repo.test_status": true, "repo.branches": true}
	scratch := map[string]bool{"repo.sync": true, "repo.start": true, "repo.edit": true, "repo.write": true, "repo.apply_patch": true, "repo.reset": true, "repo.test": true, "repo.test_start": true, "repo.test_cancel": true, "repo.build_node": true, "repo.commit": true}
	// the person's own steps on local repositories (D76): never read-only, never scratch
	manage := map[string]bool{"workshop.create": true, "workshop.clone": true, "workshop.branch": true, "workshop.merge": true, "workshop.publish": true, "workshop.pull": true, "workshop.remove": true}
	seen := 0
	for _, c := range f.w.Registry() {
		info := capability.InfoOf(c)
		seen++
		switch {
		case c.Name() == "repo.push":
			if info.ReadOnly || info.Scratch {
				t.Errorf("repo.push must need a confirmation: %+v", info)
			}
		case readOnly[c.Name()]:
			if !info.ReadOnly || info.Scratch {
				t.Errorf("%s: %+v", c.Name(), info)
			}
		case scratch[c.Name()]:
			if info.ReadOnly || !info.Scratch {
				t.Errorf("%s: %+v", c.Name(), info)
			}
		case manage[c.Name()]:
			if info.ReadOnly || info.Scratch || !info.Manage {
				t.Errorf("%s: %+v", c.Name(), info)
			}
		default:
			t.Errorf("unexpected capability %s", c.Name())
		}
		if info.Description == "" || !json.Valid(c.InputSchema()) {
			t.Errorf("%s has no description or a broken schema", c.Name())
		}
	}
	if seen != len(readOnly)+len(scratch)+len(manage)+1 {
		t.Fatalf("expected %d capabilities, found %d", len(readOnly)+len(scratch)+len(manage)+1, seen)
	}
	// without a runner there is no repo.test
	g := newFixture(t, nil)
	for _, c := range g.w.Registry() {
		if c.Name() == "repo.test" {
			t.Fatal("repo.test without a runner")
		}
	}
}

type fakeRunner struct {
	jobs   []Job
	result Result
	seen   map[string]string
}

func (r *fakeRunner) Run(_ context.Context, j Job) (Result, error) {
	r.jobs = append(r.jobs, j)
	r.seen = map[string]string{}
	_ = filepath.WalkDir(j.Dir, func(p string, d os.DirEntry, err error) error {
		if err == nil && !d.IsDir() {
			rel, _ := filepath.Rel(j.Dir, p)
			b, _ := os.ReadFile(p)
			r.seen[filepath.ToSlash(rel)] = string(b)
		}
		return nil
	})
	return r.result, nil
}

func TestRepoTestRunsTheTemplateOnACopy(t *testing.T) {
	r := &fakeRunner{result: Result{ExitCode: 1, Output: "FAILED test_x\npassword = hunter2hunter2\n", Duration: 1500 * time.Millisecond}}
	f := newFixture(t, func(o *Options) {
		o.Runner = r
		o.RunnerConfig = protocol.WorkshopRunner{
			Type: protocol.WorkshopRunnerDocker, Images: map[string]string{"make-check": "builder:1", "pytest": "py:1"},
			Network: "pkg-net", NetworkTemplates: []string{"pytest"},
		}
	})
	f.must(t, "repo.sync", repoIn{"repo": "demo"})
	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "app/new.py", "content": "x = 1\n"})
	got := f.must(t, "repo.test", repoIn{"repo": "demo"})
	if got["passed"] != false || got["exit_code"].(float64) != 1 || got["network"] != false || got["template"] != "make-check" {
		t.Fatalf("result: %v", got)
	}
	if strings.Contains(got["output"].(string), "hunter2") {
		t.Fatalf("output not masked: %v", got["output"])
	}
	j := r.jobs[0]
	if j.Image != "builder:1" || strings.Join(j.Cmd, " ") != "make check" || j.Network != "" || j.MemoryMB == 0 || j.Timeout == 0 {
		t.Fatalf("job: %+v", j)
	}
	// the copy has the changed worktree, without .git, credentials or links
	if r.seen["app/new.py"] != "x = 1\n" || r.seen["app/main.py"] == "" {
		t.Fatalf("copy: %v", keys(r.seen))
	}
	for name := range r.seen {
		if strings.HasPrefix(name, ".git/") || name == ".env" || name == "deploy/server.key" || name == "link" {
			t.Fatalf("%s was copied", name)
		}
	}
	if _, err := os.Stat(j.Dir); err == nil {
		t.Fatal("the copy was not removed")
	}
	// a template with network rights gets the network
	f.spec.Template = "pytest"
	f.ctx = WithSpec(context.Background(), f.spec)
	f.must(t, "repo.test", repoIn{"repo": "demo"})
	if r.jobs[1].Network != "pkg-net" || r.jobs[1].Cmd[0] != "pytest" {
		t.Fatalf("job: %+v", r.jobs[1])
	}
	// a template without an image, or without a template, is refused
	f.spec.Template = "go-test"
	f.ctx = WithSpec(context.Background(), f.spec)
	_, err := f.call(t, "repo.test", repoIn{"repo": "demo"})
	refused(t, err, "no image")
	f.spec.Template = "none"
	f.ctx = WithSpec(context.Background(), f.spec)
	_, err = f.call(t, "repo.test", repoIn{"repo": "demo"})
	refused(t, err, "no test template")
}

func keys(m map[string]string) []string {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	return out
}

func TestWorkshopConfigIsChecked(t *testing.T) {
	ok := protocol.WorkshopConfig{
		Dir: "/var/lib/topcoat-node/workshop", AllowedHosts: []string{"github.com"},
		Runner: &protocol.WorkshopRunner{Type: "docker", Images: map[string]string{"pytest": "python:3.12-slim"}, MemoryMB: 1024},
	}
	cfg := protocol.NodeConfig{Roles: []string{"dev"}, Runner: protocol.RunnerConfig{Type: protocol.RunnerNone}, Tools: &protocol.ToolsConfig{Workshop: &ok}}
	if err := cfg.Validate(); err != nil {
		t.Fatalf("a good config was refused: %v", err)
	}
	for name, mod := range map[string]func(*protocol.WorkshopConfig){
		"relative dir":     func(w *protocol.WorkshopConfig) { w.Dir = "relative/dir" },
		"bad host":         func(w *protocol.WorkshopConfig) { w.AllowedHosts = []string{"Git Hub"} },
		"unknown template": func(w *protocol.WorkshopConfig) { w.Runner.Images["rm-rf"] = "x" },
		"bad image":        func(w *protocol.WorkshopConfig) { w.Runner.Images["pytest"] = "x; rm -rf /" },
		"bad runner":       func(w *protocol.WorkshopConfig) { w.Runner.Type = "shell" },
		"huge memory":      func(w *protocol.WorkshopConfig) { w.Runner.MemoryMB = 10_000_000 },
	} {
		c := cfg
		w := ok
		r := *ok.Runner
		r.Images = map[string]string{"pytest": "python:3.12-slim"}
		w.Runner = &r
		mod(&w)
		c.Tools = &protocol.ToolsConfig{Workshop: &w}
		if err := c.Validate(); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}

func gitOut(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_NOSYSTEM=1")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func TestPushSendsOnlyTheShownChangeToAWorkBranch(t *testing.T) {
	f := newFixture(t, nil)
	f.must(t, "repo.sync", repoIn{"repo": "demo"})
	mainBefore := gitOut(t, f.src, "rev-parse", "main")
	f.must(t, "repo.start", repoIn{"repo": "demo", "topic": "Fix paging"})
	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "docs/NOTES.md", "content": "hello\n"})
	d := f.must(t, "repo.diff", repoIn{"repo": "demo"})
	sum, _ := d["sha256"].(string)
	if len(sum) != 64 || d["branch"] != "topcoat/fix-paging" {
		t.Fatalf("diff: %v", d)
	}
	in := func(mod func(repoIn)) repoIn {
		m := repoIn{"repo": "demo", "message": "Add notes", "branch": "topcoat/fix-paging", "diff_sha256": sum}
		if mod != nil {
			mod(m)
		}
		return m
	}

	// a refusal pushes nothing and commits nothing
	for want, mod := range map[string]func(repoIn){
		"never the default branch": func(m repoIn) { m["branch"] = "main" },
		"only a branch topcoat/":   func(m repoIn) { m["branch"] = "feature/x" },
		"not on":                   func(m repoIn) { m["branch"] = "topcoat/other" },
		"changed since":            func(m repoIn) { m["diff_sha256"] = strings.Repeat("0", 64) },
		"commit message":           func(m repoIn) { m["message"] = " " },
	} {
		_, err := f.call(t, "repo.push", in(mod))
		refused(t, err, want)
	}
	if gitOut(t, f.w.dir+"/demo", "rev-list", "--count", "origin/main..HEAD") != "0" {
		t.Fatal("a refused push committed")
	}

	// a change that grew after the diff was shown is refused too
	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "docs/MORE.md", "content": "more\n"})
	_, err := f.call(t, "repo.push", in(nil))
	refused(t, err, "changed since")
	f.must(t, "repo.reset", repoIn{"repo": "demo"})
	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "docs/NOTES.md", "content": "hello\n"})

	got := f.must(t, "repo.push", in(nil))
	if got["pushed"] != true || got["branch"] != "topcoat/fix-paging" {
		t.Fatalf("push: %v", got)
	}
	if gitOut(t, f.src, "rev-parse", "main") != mainBefore {
		t.Fatal("main moved")
	}
	if gitOut(t, f.src, "show", "topcoat/fix-paging:docs/NOTES.md") != "hello" {
		t.Fatal("the branch does not hold the change")
	}
	if gitOut(t, f.src, "log", "-1", "--format=%s", "topcoat/fix-paging") != "Add notes" {
		t.Fatal("wrong commit message")
	}
	// nothing left to push
	_, err = f.call(t, "repo.push", in(nil))
	refused(t, err, "no change")
}

func TestPushRefusesSecrets(t *testing.T) {
	f := newFixture(t, nil)
	f.must(t, "repo.sync", repoIn{"repo": "demo"})
	f.must(t, "repo.start", repoIn{"repo": "demo", "topic": "leak"})
	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "app/conf.py", "content": "API_KEY = 'sk-live-0123456789abcdef0123456789'\n"})
	d := f.must(t, "repo.diff", repoIn{"repo": "demo"})
	_, err := f.call(t, "repo.push", repoIn{"repo": "demo", "message": "Add conf", "branch": "topcoat/leak", "diff_sha256": d["sha256"]})
	refused(t, err, "look like secrets")
	if out := gitOut(t, f.src, "branch", "--list", "topcoat/*"); out != "" {
		t.Fatalf("a branch was pushed: %s", out)
	}
}

// buildRunner stands in for the container: it "builds" by writing files into /work/.out.
type buildRunner struct {
	jobs  []Job
	files map[string]string
	exit  int
}

func (r *buildRunner) Run(_ context.Context, j Job) (Result, error) {
	r.jobs = append(r.jobs, j)
	if r.exit == 0 {
		for name, text := range r.files {
			if err := os.WriteFile(filepath.Join(j.Dir, ".out", name), []byte(text), 0o644); err != nil {
				return Result{}, err
			}
		}
	}
	return Result{ExitCode: r.exit, Output: "building\nAPI_KEY=sk-live-0123456789abcdef0123456789\n"}, nil
}

func TestBuildNodeKeepsUnsignedArtefactsOfTheWorktree(t *testing.T) {
	r := &buildRunner{files: map[string]string{"topcoat-node-linux-amd64": "ELF", "SHA256SUMS": "x"}}
	f := newFixture(t, func(o *Options) {
		o.Runner = r
		o.RunnerConfig.Images = map[string]string{"build-node": "golang:1.27"}
		o.RunnerConfig.Network = "build-net"
		o.RunnerConfig.NetworkTemplates = []string{"build-node"}
	})
	write(t, f.src, "node/cmd/topcoat-node/main.go", "package main\n")
	write(t, f.src, "scripts/build-node-release.sh", "#!/usr/bin/env bash\n")
	run(t, f.src, "add", "-A")
	run(t, f.src, "commit", "-q", "-m", "node")
	f.must(t, "repo.sync", repoIn{"repo": "demo"})
	f.must(t, "repo.start", repoIn{"repo": "demo", "topic": "try build"})
	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "node/NEW.txt", "content": "changed\n"})

	for _, bad := range []string{"", "1.0 && rm -rf /", "../x", "a b"} {
		if _, err := f.call(t, "repo.build_node", repoIn{"repo": "demo", "version": bad}); err == nil {
			t.Fatalf("version %q was accepted", bad)
		}
	}
	got := f.must(t, "repo.build_node", repoIn{"repo": "demo", "version": "0.1.6-dev1"})
	if got["built"] != true || got["signed"] != false || got["network"] != true {
		t.Fatalf("build: %v", got)
	}
	if len(r.jobs) != 1 || r.jobs[0].Image != "golang:1.27" || r.jobs[0].Network != "build-net" {
		t.Fatalf("job: %+v", r.jobs)
	}
	// the version is an argument of the script, never part of the command line
	last := r.jobs[0].Cmd
	if last[len(last)-1] != "0.1.6-dev1" || strings.Contains(last[2], "0.1.6") {
		t.Fatalf("cmd: %v", last)
	}
	if strings.Contains(got["output"].(string), "sk-live") {
		t.Fatal("a secret in the build output was not masked")
	}
	body, err := os.ReadFile(filepath.Join(f.w.dir, "out", "demo", "0.1.6-dev1", "topcoat-node-linux-amd64"))
	if err != nil || string(body) != "ELF" {
		t.Fatalf("artefact: %v %q", err, body)
	}
	if _, err := os.Stat(filepath.Join(f.w.dir, ".runs")); err == nil {
		if ents, _ := os.ReadDir(filepath.Join(f.w.dir, ".runs")); len(ents) > 0 {
			t.Fatal("the scratch copy of the build was left behind")
		}
	}
	list := f.must(t, "repo.build_list", repoIn{"repo": "demo"})
	builds := list["builds"].([]any)
	if len(builds) != 1 || builds[0].(map[string]any)["version"] != "0.1.6-dev1" || len(builds[0].(map[string]any)["files"].([]any)) != 2 {
		t.Fatalf("list: %v", list)
	}
	// the worktree is untouched by the build (only the change made before)
	if st := f.must(t, "repo.status", repoIn{"repo": "demo"}); len(toStrings(st["changed"])) != 1 {
		t.Fatalf("status: %v", st)
	}
}

func TestBuildNodeNeedsAnImageAndTheNodeDirectoryAndReportsFailure(t *testing.T) {
	// no image configured: the capability is not offered at all
	f := newFixture(t, func(o *Options) { o.Runner = &fakeRunner{} })
	for _, c := range f.w.Registry() {
		if c.Name() == "repo.build_node" {
			t.Fatal("repo.build_node without an image")
		}
	}
	// a repository without node/ is refused; a failing build keeps nothing
	r := &buildRunner{exit: 2}
	g := newFixture(t, func(o *Options) {
		o.Runner = r
		o.RunnerConfig.Images = map[string]string{"build-node": "golang:1.27"}
	})
	g.must(t, "repo.sync", repoIn{"repo": "demo"})
	_, err := g.call(t, "repo.build_node", repoIn{"repo": "demo", "version": "1"})
	refused(t, err, "node/cmd/topcoat-node")
	write(t, g.src, "node/cmd/topcoat-node/main.go", "package main\n")
	run(t, g.src, "add", "-A")
	run(t, g.src, "commit", "-q", "-m", "node")
	g.must(t, "repo.sync", repoIn{"repo": "demo"})
	got := g.must(t, "repo.build_node", repoIn{"repo": "demo", "version": "1"})
	if got["built"] != false || got["exit_code"].(float64) != 2 {
		t.Fatalf("build: %v", got)
	}
	if _, err := os.Stat(filepath.Join(g.w.dir, "out", "demo", "1")); err == nil {
		t.Fatal("a failed build left artefacts")
	}
}

func TestFirstChangeMovesOntoAWorkBranch(t *testing.T) {
	f := newFixture(t, nil)
	f.must(t, "repo.sync", repoIn{"repo": "demo"})
	if st := f.must(t, "repo.status", repoIn{"repo": "demo"}); strings.HasPrefix(st["branch"].(string), "topcoat/") {
		t.Fatalf("the fresh worktree is already on a work branch: %v", st)
	}
	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "hello.py", "content": "x = 1\n"})
	st := f.must(t, "repo.status", repoIn{"repo": "demo"})
	if b, _ := st["branch"].(string); !strings.HasPrefix(b, "topcoat/work-") {
		t.Fatalf("branch after the first write: %v", st)
	}
	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "b.py", "content": "y = 2\n"})
	if got := f.must(t, "repo.status", repoIn{"repo": "demo"}); got["branch"] != st["branch"] {
		t.Fatalf("a second write moved the branch: %v vs %v", got, st)
	}
}
