package workshop

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// onBranch commits files to a local branch of the clone with git's plumbing (no checkout, so
// the worktree stays as it is): the branch starts at from, each file is a blob of the given mode.
func onBranch(t *testing.T, clone, branch, from string, files map[string][2]string) {
	t.Helper()
	index := filepath.Join(t.TempDir(), "index")
	env := []string{"GIT_INDEX_FILE=" + index}
	runEnv(t, clone, env, "read-tree", from)
	for rel, mf := range files {
		blob := filepath.Join(t.TempDir(), "blob")
		if err := os.WriteFile(blob, []byte(mf[1]), 0o644); err != nil {
			t.Fatal(err)
		}
		sha := gitOutEnv(t, clone, nil, "hash-object", "-w", blob)
		runEnv(t, clone, env, "update-index", "--add", "--cacheinfo", mf[0]+","+sha+","+rel)
	}
	tree := gitOutEnv(t, clone, env, "write-tree")
	commit := gitOutEnv(t, clone, nil, "commit-tree", tree, "-p", from, "-m", "sub-task")
	run(t, clone, "update-ref", "refs/heads/"+branch, commit)
}

func execGit(dir string, args ...string) *exec.Cmd {
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_NOSYSTEM=1",
		"GIT_AUTHOR_NAME=T", "GIT_AUTHOR_EMAIL=t@example.invalid", "GIT_COMMITTER_NAME=T", "GIT_COMMITTER_EMAIL=t@example.invalid")
	return cmd
}

func runEnv(t *testing.T, dir string, env []string, args ...string) {
	t.Helper()
	gitOutEnv(t, dir, env, args...)
}

func gitOutEnv(t *testing.T, dir string, env []string, args ...string) string {
	t.Helper()
	cmd := execGit(dir, args...)
	cmd.Env = append(cmd.Env, env...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func TestStartContinuesALargeTasksBranch(t *testing.T) {
	f := newFixture(t, nil)
	f.must(t, "repo.sync", repoIn{"repo": "demo"})
	clone := filepath.Join(f.w.dir, "demo")

	// the first sub-task: no branch yet, so it starts fresh from the default branch
	first := f.must(t, "repo.start", repoIn{"repo": "demo", "topic": "v-7", "continue": true})
	if first["branch"] != "topcoat/v-7" || first["based_on"] != "origin/main" || first["continued"] != false {
		t.Fatalf("first sub-task: %v", first)
	}
	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "step1.txt", "content": "one\n"})
	run(t, clone, "add", "-A")
	run(t, clone, "commit", "-q", "-m", "step 1")
	step1 := gitOut(t, clone, "rev-parse", "HEAD")

	// the worktree moves elsewhere in between (another issue, say)
	f.must(t, "repo.start", repoIn{"repo": "demo", "topic": "other"})

	// the next sub-task builds on the first one's commit
	next := f.must(t, "repo.start", repoIn{"repo": "demo", "topic": "V 7", "continue": true})
	if next["branch"] != "topcoat/v-7" || next["based_on"] != "topcoat/v-7" || next["continued"] != true {
		t.Fatalf("continued sub-task: %v", next)
	}
	if head := gitOut(t, clone, "rev-parse", "HEAD"); head != step1 {
		t.Fatalf("the branch was reset: HEAD %s, want %s", head, step1)
	}
	if st := f.must(t, "repo.status", repoIn{"repo": "demo"}); st["branch"] != "topcoat/v-7" {
		t.Fatalf("status: %v", st)
	}

	// unsaved changes still need discard, as for any start
	f.must(t, "repo.write", repoIn{"repo": "demo", "path": "half.txt", "content": "x"})
	_, err := f.call(t, "repo.start", repoIn{"repo": "demo", "topic": "v-7", "continue": true})
	refused(t, err, "has changes")
	f.must(t, "repo.start", repoIn{"repo": "demo", "topic": "v-7", "continue": true, "discard": true})
	if head := gitOut(t, clone, "rev-parse", "HEAD"); head != step1 {
		t.Fatalf("discard lost the sub-task's commit: HEAD %s", head)
	}

	// without the flag a large task's topic starts over, as before
	fresh := f.must(t, "repo.start", repoIn{"repo": "demo", "topic": "v-7"})
	if _, ok := fresh["continued"]; ok || fresh["based_on"] != "origin/main" {
		t.Fatalf("start without continue: %v", fresh)
	}
	if head := gitOut(t, clone, "rev-parse", "HEAD"); head == step1 {
		t.Fatal("start without continue kept the branch")
	}

	// continue is only for a large task's topic
	for _, topic := range []string{"fix paging", "issue-12", "va-1"} {
		_, err := f.call(t, "repo.start", repoIn{"repo": "demo", "topic": topic, "continue": true})
		refused(t, err, "large task")
	}
}

func TestReadsOnALargeTasksBranch(t *testing.T) {
	f := newFixture(t, nil)
	f.must(t, "repo.sync", repoIn{"repo": "demo"})
	clone := filepath.Join(f.w.dir, "demo")
	big := strings.Repeat("x = 1\n", maxFileBytes/6+10)
	files := map[string][2]string{
		"app/new.py":      {"100644", "def added():\n    return 1\n\n\nclass Shape:\n    pass\n"},
		"app/main.py":     {"100644", "def greet(name):\n    return 'Hi ' + name\n"},
		"app/settings.py": {"100644", "TOKEN = 'ghp_0123456789abcdefghij0123456789abcdef'\n"},
		"app/.env":        {"100644", "API_KEY=sk-live-0123456789abcdef0123456789\n"},
		"app/alias.py":    {"120000", "../config.py"},
		"app/big.py":      {"100644", big},
		"app/bin.dat":     {"100644", "a\x00b"},
		"lib/sub":         {"160000", "0123456789abcdef0123456789abcdef01234567"},
	}
	// a name that reads as pathspec magic (Windows allows no colon in a path)
	odd := runtime.GOOS != "windows"
	if odd {
		files[":odd.md"] = [2]string{"100644", "# odd name\n"}
	}
	onBranch(t, clone, "topcoat/v-3", "HEAD", files)
	before := gitOut(t, clone, "rev-parse", "HEAD")

	// a file that exists only on the branch, and the branch's version of a changed one
	got := f.must(t, "repo.read", repoIn{"repo": "demo", "path": "app/new.py", "branch": "topcoat/v-3"})
	if got["path"] != "app/new.py" || got["branch"] != "topcoat/v-3" || !strings.HasPrefix(got["text"].(string), "def added():") {
		t.Fatalf("read on the branch: %v", got)
	}
	got = f.must(t, "repo.read", repoIn{"repo": "demo", "path": "app/main.py", "branch": "topcoat/v-3", "start_line": 2})
	if got["text"] != "    return 'Hi ' + name" || got["total_lines"].(float64) != 2 {
		t.Fatalf("line range on the branch: %v", got)
	}
	if wt := f.must(t, "repo.read", repoIn{"repo": "demo", "path": "app/main.py"}); !strings.Contains(wt["text"].(string), "'Hello '") {
		t.Fatalf("without branch the worktree is read: %v", wt)
	}
	_, err := f.call(t, "repo.read", repoIn{"repo": "demo", "path": "app/new.py"})
	refused(t, err, "not a file")
	// secret-looking values are masked as in the worktree
	got = f.must(t, "repo.read", repoIn{"repo": "demo", "path": "app/settings.py", "branch": "topcoat/v-3"})
	if strings.Contains(got["text"].(string), "ghp_0123") || got["masked"].(float64) < 1 {
		t.Fatalf("not masked: %v", got)
	}
	// a name is a name, not a pathspec
	if odd {
		got = f.must(t, "repo.read", repoIn{"repo": "demo", "path": ":odd.md", "branch": "topcoat/v-3"})
		if got["text"] != "# odd name" {
			t.Fatalf("odd name: %v", got)
		}
	}

	for path, why := range map[string]string{
		"app/.env":     "credentials",
		".env":         "credentials",
		"app/alias.py": "not a file",
		"lib/sub":      "not a file",
		"app/big.py":   "too large",
		"app/bin.dat":  "not a UTF-8 text file",
		"app":          "not a file",
		".git/config":  "not a file",
		"../outside":   "inside the repository",
		"nope.py":      "not a file",
	} {
		_, err := f.call(t, "repo.read", repoIn{"repo": "demo", "path": path, "branch": "topcoat/v-3"})
		if err == nil {
			t.Fatalf("%s was readable on the branch", path)
		}
		refused(t, err, why)
	}

	run(t, clone, "branch", "topcoat/x")
	for branch, why := range map[string]string{
		"main": "large task", "topcoat/x": "large task", "../": "large task", "topcoat/v-3/../x": "large task",
		"refs/heads/topcoat/v-3": "large task", "topcoat/v-3^": "large task", "topcoat/v-9": "no branch",
	} {
		_, err := f.call(t, "repo.read", repoIn{"repo": "demo", "path": "README.md", "branch": branch})
		if err == nil {
			t.Fatalf("branch %q was read", branch)
		}
		refused(t, err, why)
		_, err = f.call(t, "repo.outline", repoIn{"repo": "demo", "branch": branch})
		refused(t, err, why)
	}

	// the outline maps the branch: plain files of known kinds, no secrets, no link, no submodule
	var out struct {
		Branch string        `json:"branch"`
		Files  []outlineFile `json:"files"`
	}
	remarshal(t, f.must(t, "repo.outline", repoIn{"repo": "demo", "path": "app", "branch": "topcoat/v-3"}), &out)
	var paths []string
	for _, file := range out.Files {
		paths = append(paths, file.Path)
		if file.Path == "app/new.py" && (len(file.Symbols) != 2 || file.Symbols[1].Name != "Shape" || file.Symbols[1].Start != 5) {
			t.Fatalf("app/new.py: %+v", file)
		}
		if file.Path == "app/big.py" && file.Skipped != "too large" {
			t.Fatalf("app/big.py: %+v", file)
		}
	}
	sameList(t, paths, []string{"app/big.py", "app/main.py", "app/new.py", "app/settings.py", "app/util.py"})
	if out.Branch != "topcoat/v-3" {
		t.Fatalf("outline does not name the branch: %+v", out)
	}

	// nothing was checked out for the reads
	if head := gitOut(t, clone, "rev-parse", "HEAD"); head != before {
		t.Fatalf("HEAD moved: %s", head)
	}
	if st := f.must(t, "repo.status", repoIn{"repo": "demo"}); st["clean"] != true || st["branch"] != "main" {
		t.Fatalf("the worktree changed: %v", st)
	}
}

// git's output is capped; an entry cut at the cap must not be read under its cut name. The listing
// is padded so the cap falls inside the name of a secret file, right after "zz.md" of "zz.md.env".
func TestBranchListingCutAtTheCapIsDropped(t *testing.T) {
	f := newFixture(t, nil)
	f.must(t, "repo.sync", repoIn{"repo": "demo"})
	clone := filepath.Join(f.w.dir, "demo")
	hash := func(content string) string {
		cmd := execGit(clone, "hash-object", "-w", "--stdin")
		cmd.Stdin = strings.NewReader(content)
		out, err := cmd.Output()
		if err != nil {
			t.Fatal(err)
		}
		return strings.TrimSpace(string(out))
	}
	pad, secret := hash("x"), hash("# db password hunter2\n")
	// a line of `ls-tree -r -z --long` is 62 bytes plus the path (sizes below 10^7); the cap must
	// fall after "zz.md" in the secret's line, before its NUL
	const width = 200
	before := gitOutputLimit - (61 + len("zz.md"))
	n := before/(62+width) - 1
	var tree strings.Builder
	for i := range n {
		fmt.Fprintf(&tree, "100644 blob %s\tp%0*d\n", pad, width-1, i)
	}
	fmt.Fprintf(&tree, "100644 blob %s\tq%s\n", pad, strings.Repeat("a", before-n*(62+width)-63))
	fmt.Fprintf(&tree, "100644 blob %s\tzz.md.env\n", secret)
	cmd := execGit(clone, "mktree")
	cmd.Stdin = strings.NewReader(tree.String())
	out, err := cmd.Output()
	if err != nil {
		t.Fatal(err)
	}
	commit := gitOutEnv(t, clone, nil, "commit-tree", strings.TrimSpace(string(out)), "-m", "padded")
	run(t, clone, "update-ref", "refs/heads/topcoat/v-1", commit)

	got := f.must(t, "repo.outline", repoIn{"repo": "demo", "branch": "topcoat/v-1", "kinds": []string{"md"}})
	if files, _ := got["files"].([]any); len(files) != 0 {
		t.Fatalf("a cut entry was outlined: %v", got)
	}
}
