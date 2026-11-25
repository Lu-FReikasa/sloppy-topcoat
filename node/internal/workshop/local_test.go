package workshop

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"topcoat/node/internal/providers/capability"
)

// The tests of local repositories (D76) build their remotes with plain git and use no symlinks,
// so they run on every platform.

func localWorkshop(t *testing.T) *Workshop {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git is not installed")
	}
	w, err := New(Options{Dir: filepath.Join(t.TempDir(), "ws"), allowFileURLs: true})
	if err != nil {
		t.Fatal(err)
	}
	return w
}

func capOf(t *testing.T, w *Workshop, name string) capability.Capability {
	t.Helper()
	for _, c := range w.Registry() {
		if c.Name() == name {
			return c
		}
	}
	t.Fatalf("no capability %s", name)
	return nil
}

func call(t *testing.T, w *Workshop, ctx context.Context, name, input string) (map[string]any, error) {
	t.Helper()
	raw, err := capOf(t, w, name).Invoke(ctx, json.RawMessage(input))
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	return out, nil
}

func must(t *testing.T, w *Workshop, ctx context.Context, name, input string) map[string]any {
	t.Helper()
	out, err := call(t, w, ctx, name, input)
	if err != nil {
		t.Fatalf("%s %s: %v", name, input, err)
	}
	return out
}

func gitIn(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@example.invalid",
		"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@example.invalid", "GIT_CONFIG_GLOBAL="+os.DevNull)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

// remoteRepo is a bare repository with one commit on main, as a stand-in for GitHub.
func remoteRepo(t *testing.T) (bare, url string) {
	t.Helper()
	root := t.TempDir()
	bare = filepath.Join(root, "remote.git")
	gitIn(t, root, "init", "--bare", "--initial-branch=main", bare)
	work := filepath.Join(root, "work")
	gitIn(t, root, "clone", bare, work)
	if err := os.WriteFile(filepath.Join(work, "app.txt"), []byte("one\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	gitIn(t, work, "add", "-A")
	gitIn(t, work, "commit", "-m", "first")
	gitIn(t, work, "push", "origin", "HEAD:main")
	return bare, "file:///" + filepath.ToSlash(bare)
}

func localCtx(branch string) context.Context {
	return WithSpec(context.Background(), Spec{Local: true, Branch: branch})
}

func branchNames(out map[string]any) []string {
	var names []string
	for _, b := range out["branches"].([]any) {
		names = append(names, b.(map[string]any)["name"].(string))
	}
	return names
}

func TestTheManagementStepsAreMarkedAndNotReadOnly(t *testing.T) {
	w := localWorkshop(t)
	for _, name := range []string{"workshop.create", "workshop.clone", "workshop.branch", "workshop.merge",
		"workshop.publish", "workshop.pull", "workshop.remove"} {
		info := capability.InfoOf(capOf(t, w, name))
		if !info.Manage || info.ReadOnly || info.Scratch {
			t.Fatalf("%s: %+v", name, info)
		}
	}
	if info := capability.InfoOf(capOf(t, w, "repo.branches")); !info.ReadOnly || info.Manage {
		t.Fatalf("repo.branches: %+v", info)
	}
	if info := capability.InfoOf(capOf(t, w, "repo.commit")); !info.Scratch || info.Manage {
		t.Fatalf("repo.commit: %+v", info)
	}
}

func TestANewLocalRepositoryStartsWithAReadmeAndIsWorkedOnInTheWorkshop(t *testing.T) {
	w := localWorkshop(t)
	ctx := context.Background()
	out := must(t, w, ctx, "workshop.create", `{"repo":"notes","title":"Notes","description":"A small tool."}`)
	if out["default_branch"] != "main" || out["created"] != true {
		t.Fatalf("%v", out)
	}
	if _, err := call(t, w, ctx, "workshop.create", `{"repo":"notes"}`); err == nil {
		t.Fatal("created twice")
	}
	if _, err := call(t, w, ctx, "workshop.create", `{"repo":"../escape"}`); err == nil {
		t.Fatal("a path as a name")
	}
	if _, err := call(t, w, ctx, "workshop.create", `{"repo":"leak","description":"password: hunter2hunter2"}`); err == nil {
		t.Fatal("a secret in the README")
	}
	// the workshop's clone comes from the local origin, never from a path the hub names
	lc := localCtx("main")
	must(t, w, lc, "repo.sync", `{"repo":"notes"}`)
	read := must(t, w, lc, "repo.read", `{"repo":"notes","path":"README.md"}`)
	if !strings.Contains(read["text"].(string), "A small tool.") {
		t.Fatalf("%v", read)
	}
	// an assistant works on a branch topcoat/* and keeps its work there, never on main
	must(t, w, lc, "repo.start", `{"repo":"notes","topic":"add todo"}`)
	must(t, w, lc, "repo.write", `{"repo":"notes","path":"todo.md","content":"- milk\n"}`)
	kept := must(t, w, lc, "repo.commit", `{"repo":"notes","message":"Add a todo list"}`)
	if kept["branch"] != "topcoat/add-todo" {
		t.Fatalf("%v", kept)
	}
	branches := must(t, w, lc, "repo.branches", `{"repo":"notes"}`)
	if got := branchNames(branches); len(got) != 2 {
		t.Fatalf("%v", got)
	}
	for _, b := range branches["branches"].([]any) {
		m := b.(map[string]any)
		if m["name"] == "topcoat/add-todo" && (m["ahead"].(float64) != 1 || m["work"] != true) {
			t.Fatalf("%v", m)
		}
	}
	// a secret is not kept
	must(t, w, lc, "repo.write", `{"repo":"notes","path":"cfg.txt","content":"token = ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"}`)
	if _, err := call(t, w, lc, "repo.commit", `{"repo":"notes","message":"cfg"}`); err == nil {
		t.Fatal("kept a secret")
	}
}

func TestRepoCommitIsForLocalRepositoriesOnly(t *testing.T) {
	w := localWorkshop(t)
	_, url := remoteRepo(t)
	remote := WithSpec(context.Background(), Spec{URL: url, Branch: "main"})
	must(t, w, remote, "repo.sync", `{"repo":"app"}`)
	must(t, w, remote, "repo.start", `{"repo":"app","topic":"try-b"}`)
	must(t, w, remote, "repo.write", `{"repo":"app","path":"b.txt","content":"b\n"}`)
	if _, err := call(t, w, remote, "repo.commit", `{"repo":"app","message":"b file"}`); err == nil ||
		!strings.Contains(err.Error(), "repo.push") {
		t.Fatalf("%v", err)
	}
}

func TestABranchIsTakenIntoMainByAFastForwardOrAMergeAndAConflictChangesNothing(t *testing.T) {
	w := localWorkshop(t)
	ctx := context.Background()
	must(t, w, ctx, "workshop.create", `{"repo":"site"}`)
	must(t, w, ctx, "workshop.branch", `{"repo":"site","name":"feature"}`)
	if _, err := call(t, w, ctx, "workshop.branch", `{"repo":"site","name":"feature"}`); err == nil {
		t.Fatal("the same branch twice")
	}
	if _, err := call(t, w, ctx, "workshop.branch", `{"repo":"site","name":"x","from":"nowhere"}`); err == nil {
		t.Fatal("from a branch that is not there")
	}
	origin, _ := w.localOrigin("site")
	work := filepath.Join(t.TempDir(), "w")
	gitIn(t, filepath.Dir(work), "clone", origin, work)
	commit := func(branch, file, text string) {
		gitIn(t, work, "checkout", "-q", branch)
		if err := os.WriteFile(filepath.Join(work, file), []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
		gitIn(t, work, "add", "-A")
		gitIn(t, work, "commit", "-q", "-m", file)
		gitIn(t, work, "push", "-q", "origin", branch)
	}
	commit("feature", "a.txt", "a\n")
	out := must(t, w, ctx, "workshop.merge", `{"repo":"site","branch":"feature"}`)
	if out["merged"] != true || out["fast_forward"] != true || out["into"] != "main" {
		t.Fatalf("%v", out)
	}
	if again := must(t, w, ctx, "workshop.merge", `{"repo":"site","branch":"feature"}`); again["merged"] != false {
		t.Fatalf("%v", again)
	}
	// both went on: a merge commit
	gitIn(t, work, "fetch", "-q", "origin")
	gitIn(t, work, "checkout", "-q", "-B", "main", "origin/main")
	commit("main", "m.txt", "m\n")
	gitIn(t, work, "checkout", "-q", "-B", "feature", "origin/feature")
	commit("feature", "f.txt", "f\n")
	out = must(t, w, ctx, "workshop.merge", `{"repo":"site","branch":"feature"}`)
	if out["merged"] != true || out["fast_forward"] != false {
		t.Fatalf("%v", out)
	}
	// the same lines changed twice: nothing is taken, the files are named
	gitIn(t, work, "fetch", "-q", "origin")
	gitIn(t, work, "checkout", "-q", "-B", "main", "origin/main")
	commit("main", "README.md", "main says\n")
	gitIn(t, work, "checkout", "-q", "-B", "feature", "origin/feature")
	commit("feature", "README.md", "feature says\n")
	before := gitIn(t, origin, "rev-parse", "main")
	out = must(t, w, ctx, "workshop.merge", `{"repo":"site","branch":"feature"}`)
	if out["merged"] != false || len(out["conflicts"].([]any)) != 1 {
		t.Fatalf("%v", out)
	}
	if after := gitIn(t, origin, "rev-parse", "main"); after != before {
		t.Fatal("a conflict changed main")
	}
	if entries, _ := os.ReadDir(filepath.Join(w.dir, ".merge")); len(entries) != 0 {
		t.Fatalf("the merge worktree was left behind: %v", entries)
	}
}

func TestALocalCloneOfARemoteRepositoryIsPublishedAndUpdatedWithoutForce(t *testing.T) {
	w := localWorkshop(t)
	bare, url := remoteRepo(t)
	remote := WithSpec(context.Background(), Spec{URL: url, Branch: "main"})
	out := must(t, w, remote, "workshop.clone", `{"repo":"app"}`)
	if out["default_branch"] != "main" || out["created"] != true {
		t.Fatalf("%v", out)
	}
	origin, _ := w.localOrigin("app")
	if remotes := gitIn(t, origin, "remote"); remotes != "" {
		t.Fatalf("the copy keeps a remote: %q", remotes)
	}
	// a branch made here is published to the remote
	must(t, w, context.Background(), "workshop.branch", `{"repo":"app","name":"topcoat/try"}`)
	pub := must(t, w, remote, "workshop.publish", `{"repo":"app","branch":"topcoat/try"}`)
	if pub["published"] != true {
		t.Fatalf("%v", pub)
	}
	if got := gitIn(t, bare, "branch", "--list", "topcoat/try"); !strings.Contains(got, "topcoat/try") {
		t.Fatalf("not on the remote: %q", got)
	}
	// the remote goes on: pull brings main up to date by a fast-forward
	work := filepath.Join(t.TempDir(), "w")
	gitIn(t, filepath.Dir(work), "clone", bare, work)
	if err := os.WriteFile(filepath.Join(work, "app.txt"), []byte("two\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	gitIn(t, work, "commit", "-qam", "second")
	gitIn(t, work, "push", "-q", "origin", "HEAD:main")
	if up := must(t, w, remote, "workshop.pull", `{"repo":"app","branch":"main"}`); up["updated"] != true {
		t.Fatalf("%v", up)
	}
	// a main that went its own way here is not published over the remote's (never forced)
	local := filepath.Join(t.TempDir(), "l")
	gitIn(t, filepath.Dir(local), "clone", origin, local)
	if err := os.WriteFile(filepath.Join(local, "here.txt"), []byte("here\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	gitIn(t, local, "add", "-A")
	gitIn(t, local, "commit", "-qm", "here")
	gitIn(t, local, "push", "-q", "origin", "HEAD:main")
	if err := os.WriteFile(filepath.Join(work, "app.txt"), []byte("three\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	gitIn(t, work, "commit", "-qam", "third")
	gitIn(t, work, "push", "-q", "origin", "HEAD:main")
	if _, err := call(t, w, remote, "workshop.publish", `{"repo":"app","branch":"main"}`); err == nil {
		t.Fatal("published over a remote that moved on")
	}
	if up := must(t, w, remote, "workshop.pull", `{"repo":"app","branch":"main"}`); up["updated"] != false {
		t.Fatalf("pulled over local work: %v", up)
	}
	// without the remote's address nothing is published
	if _, err := call(t, w, context.Background(), "workshop.publish", `{"repo":"app","branch":"main"}`); err == nil {
		t.Fatal("published without a remote")
	}
}

func TestARemovedLocalRepositoryIsGoneAndNeedsAConfirmation(t *testing.T) {
	w := localWorkshop(t)
	ctx := context.Background()
	must(t, w, ctx, "workshop.create", `{"repo":"tmp"}`)
	must(t, w, localCtx("main"), "repo.sync", `{"repo":"tmp"}`)
	if _, err := call(t, w, ctx, "workshop.remove", `{"repo":"tmp","confirm":false}`); err == nil {
		t.Fatal("removed without confirmation")
	}
	must(t, w, ctx, "workshop.remove", `{"repo":"tmp","confirm":true}`)
	if _, err := w.localOrigin("tmp"); err == nil {
		t.Fatal("the origin is still there")
	}
	if _, err := os.Stat(filepath.Join(w.dir, "tmp")); err == nil {
		t.Fatal("the clone is still there")
	}
}
