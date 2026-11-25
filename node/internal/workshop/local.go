package workshop

// Local repositories (ARCHITECTURE D76): the origin of a repository can live in this workshop
// (`<workshop>/.origin/<name>.git`, a bare repository) instead of on GitHub. The workshop's own
// clone comes from it, an assistant works on branches `topcoat/*` and commits them there, and the
// person decides on the hub's page what reaches the default branch and what is published.
//
// The person's steps are capabilities marked manage: only the action workshop.manage runs them,
// never an assistant (capability.invoke / capability.execute refuse them). None of them takes a
// path: the origin is computed from the repository's name; a remote is an https URL from the
// allow-list, with the token in the environment of one git call, as for every clone.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/tools"
)

const (
	maxDescription = 2000
	maxBranches    = 200
)

// manageCapabilities are the person's own steps on local repositories (D76).
func (w *Workshop) manageCapabilities() []capability.Capability {
	return []capability.Capability{
		w.capCreate(), w.capCloneLocal(), w.capBranch(), w.capMerge(), w.capPublish(), w.capPull(),
		w.capRemove(),
	}
}

// bare runs git in the origin of a local repository.
func (w *Workshop) bare(ctx context.Context, origin string, auth *authHeader, stdin string, args ...string) (string, error) {
	return w.git(ctx, origin, auth, stdin, args...)
}

func (w *Workshop) identity(ctx context.Context, dir string) error {
	for _, kv := range [][2]string{{"user.name", "Topcoat Workshop"}, {"user.email", "workshop@topcoat.invalid"}} {
		if _, err := w.git(ctx, dir, nil, "", "config", kv[0], kv[1]); err != nil {
			return err
		}
	}
	return nil
}

func (w *Workshop) branchExists(ctx context.Context, origin, branch string) bool {
	_, err := w.bare(ctx, origin, nil, "", "rev-parse", "--verify", "--quiet", "refs/heads/"+branch)
	return err == nil
}

func (w *Workshop) defaultBranch(ctx context.Context, origin string) string {
	out, err := w.bare(ctx, origin, nil, "", "symbolic-ref", "--short", "HEAD")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(out)
}

// --- workshop.create -------------------------------------------------------------------------

func (w *Workshop) capCreate() capability.Capability {
	return wcap{
		name: "workshop.create", manage: true,
		desc: "Create a new local repository in this workshop with a first commit (a README).",
		schema: `{"type":"object","additionalProperties":false,"required":["repo"],"properties":{` + repoProp + `,` +
			`"branch":{"type":"string","maxLength":100,"description":"the default branch (main)"},` +
			`"title":{"type":"string","maxLength":200},` +
			`"description":{"type":"string","maxLength":2000}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Repo, Branch, Title, Description string
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			if in.Branch == "" {
				in.Branch = "main"
			}
			if err := checkBranch(in.Branch); err != nil {
				return nil, err
			}
			origin, err := w.originDir(in.Repo)
			if err != nil {
				return nil, err
			}
			unlock := w.lock(in.Repo)
			defer unlock()
			if _, err := os.Stat(origin); err == nil {
				return nil, inputErr("a local repository %q exists already", in.Repo)
			}
			if err := os.MkdirAll(filepath.Dir(origin), 0o700); err != nil {
				return nil, err
			}
			if _, err := w.git(ctx, "", nil, "", "init", "--bare", "--quiet", "--initial-branch="+in.Branch, "--", origin); err != nil {
				return nil, err
			}
			title := strings.TrimSpace(in.Title)
			if title == "" {
				title = in.Repo
			}
			readme := "# " + title + "\n"
			if d := strings.TrimSpace(in.Description); d != "" {
				readme += "\n" + d + "\n"
			}
			if _, n := tools.Mask(readme); n > 0 {
				_ = os.RemoveAll(origin)
				return nil, inputErr("the description contains a value that looks like a secret")
			}
			commit, err := w.firstCommit(ctx, origin, in.Branch, readme)
			if err != nil {
				_ = os.RemoveAll(origin)
				return nil, err
			}
			return map[string]any{"repo": in.Repo, "default_branch": in.Branch, "commit": commit, "created": true}, nil
		},
	}
}

// firstCommit writes README.md as the first commit of a bare repository, without a worktree.
func (w *Workshop) firstCommit(ctx context.Context, origin, branch, readme string) (string, error) {
	if err := w.identity(ctx, origin); err != nil {
		return "", err
	}
	blob, err := w.bare(ctx, origin, nil, readme, "hash-object", "-w", "--stdin")
	if err != nil {
		return "", err
	}
	tree, err := w.bare(ctx, origin, nil, "100644 blob "+strings.TrimSpace(blob)+"\tREADME.md\n", "mktree")
	if err != nil {
		return "", err
	}
	commit, err := w.bare(ctx, origin, nil, "Create the repository\n", "commit-tree", strings.TrimSpace(tree))
	if err != nil {
		return "", err
	}
	commit = strings.TrimSpace(commit)
	if _, err := w.bare(ctx, origin, nil, "", "update-ref", "refs/heads/"+branch, commit); err != nil {
		return "", err
	}
	return commit[:12], nil
}

// --- workshop.clone ---------------------------------------------------------------------------

// capCloneLocal makes a local repository from a remote one (a local clone of an external
// repository, for editing and testing here): the remote comes as the call's Spec.
func (w *Workshop) capCloneLocal() capability.Capability {
	return wcap{
		name: "workshop.clone", manage: true,
		desc:   "Create a local repository as a full copy of a remote one (the call's repository), with all its branches.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo"],"properties":{` + repoProp + `}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct{ Repo string }
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			spec, ok := specOf(ctx)
			if !ok {
				return nil, inputErr("the hub did not say which remote repository to copy")
			}
			if err := w.checkRemote(spec); err != nil {
				return nil, err
			}
			origin, err := w.originDir(in.Repo)
			if err != nil {
				return nil, err
			}
			unlock := w.lock(in.Repo)
			defer unlock()
			if _, err := os.Stat(origin); err == nil {
				return nil, inputErr("a local repository %q exists already", in.Repo)
			}
			if err := os.MkdirAll(filepath.Dir(origin), 0o700); err != nil {
				return nil, err
			}
			if _, err := w.git(ctx, "", authFor(spec), "", "clone", "--bare", "--quiet", "--", spec.URL, origin); err != nil {
				_ = os.RemoveAll(origin)
				return nil, fmt.Errorf("clone failed: %w", err)
			}
			if mb := dirSizeMB(origin); mb > w.o.MaxRepoMB {
				_ = os.RemoveAll(origin)
				return nil, inputErr("the repository is %d MB, more than the %d MB this workshop keeps", mb, w.o.MaxRepoMB)
			}
			// the remote is not kept in the copy: publishing names it again with each call
			_, _ = w.bare(ctx, origin, nil, "", "remote", "remove", "origin")
			if err := w.identity(ctx, origin); err != nil {
				return nil, err
			}
			branches, _ := w.listBranches(ctx, origin)
			return map[string]any{
				"repo": in.Repo, "default_branch": w.defaultBranch(ctx, origin), "branches": len(branches), "created": true,
			}, nil
		},
	}
}

// --- repo.branches -----------------------------------------------------------------------------

type branchInfo struct {
	Name    string `json:"name"`
	Commit  string `json:"commit"`
	Date    string `json:"date"`
	Subject string `json:"subject"`
	Ahead   int    `json:"ahead"`
	Behind  int    `json:"behind"`
	Work    bool   `json:"work"`
	Default bool   `json:"default"`
}

func (w *Workshop) listBranches(ctx context.Context, origin string) ([]branchInfo, error) {
	out, err := w.bare(ctx, origin, nil, "", "for-each-ref", "--sort=-committerdate", "--count="+strconv.Itoa(maxBranches),
		"--format=%(refname:short)%09%(objectname:short=12)%09%(committerdate:iso8601-strict)%09%(contents:subject)", "refs/heads")
	if err != nil {
		return nil, err
	}
	def := w.defaultBranch(ctx, origin)
	var list []branchInfo
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		parts := strings.SplitN(line, "\t", 4)
		if len(parts) < 4 {
			continue
		}
		b := branchInfo{Name: parts[0], Commit: parts[1], Date: parts[2], Subject: cutRunes(parts[3], 200)}
		b.Default = b.Name == def
		b.Work = strings.HasPrefix(b.Name, "topcoat/")
		if !b.Default && def != "" {
			counts, err := w.bare(ctx, origin, nil, "", "rev-list", "--left-right", "--count", def+"..."+b.Name)
			if err == nil {
				f := strings.Fields(counts)
				if len(f) == 2 {
					b.Behind, _ = strconv.Atoi(f[0])
					b.Ahead, _ = strconv.Atoi(f[1])
				}
			}
		}
		list = append(list, b)
	}
	return list, nil
}

func (w *Workshop) capBranches() capability.Capability {
	return wcap{
		name: "repo.branches", readOnly: true,
		desc:   "List the branches of a local repository: last commit, how far each is ahead of and behind the default branch, which are work branches (topcoat/*).",
		schema: `{"type":"object","additionalProperties":false,"required":["repo"],"properties":{` + repoProp + `}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct{ Repo string }
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			origin, err := w.localOrigin(in.Repo)
			if err != nil {
				return nil, err
			}
			list, err := w.listBranches(ctx, origin)
			if err != nil {
				return nil, err
			}
			return map[string]any{"repo": in.Repo, "default_branch": w.defaultBranch(ctx, origin), "branches": list}, nil
		},
	}
}

// --- workshop.branch ---------------------------------------------------------------------------

func (w *Workshop) capBranch() capability.Capability {
	return wcap{
		name: "workshop.branch", manage: true,
		desc: "Create a branch in a local repository, from the default branch or another branch.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo","name"],"properties":{` + repoProp + `,` +
			`"name":{"type":"string","maxLength":100},"from":{"type":"string","maxLength":100}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct{ Repo, Name, From string }
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			origin, err := w.localOrigin(in.Repo)
			if err != nil {
				return nil, err
			}
			if err := checkBranch(in.Name); err != nil {
				return nil, err
			}
			from := in.From
			if from == "" {
				from = w.defaultBranch(ctx, origin)
			}
			if err := checkBranch(from); err != nil {
				return nil, err
			}
			unlock := w.lock(in.Repo)
			defer unlock()
			if w.branchExists(ctx, origin, in.Name) {
				return nil, inputErr("the branch %q exists already", in.Name)
			}
			if !w.branchExists(ctx, origin, from) {
				return nil, inputErr("there is no branch %q to start from", from)
			}
			if _, err := w.bare(ctx, origin, nil, "", "branch", "--", in.Name, from); err != nil {
				return nil, err
			}
			return map[string]any{"repo": in.Repo, "branch": in.Name, "from": from, "created": true}, nil
		},
	}
}

// --- workshop.merge ----------------------------------------------------------------------------

// capMerge takes a branch into another (the default branch unless named): the person's click on
// the hub's page after they saw the change. Fast-forward when possible, a merge commit otherwise;
// a conflict changes nothing and names the files.
func (w *Workshop) capMerge() capability.Capability {
	return wcap{
		name: "workshop.merge", manage: true,
		desc: "Take a branch of a local repository into the default branch (or another one): fast-forward or a merge commit; a conflict changes nothing.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo","branch"],"properties":{` + repoProp + `,` +
			`"branch":{"type":"string","maxLength":100},"into":{"type":"string","maxLength":100},` +
			`"message":{"type":"string","maxLength":2000}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct{ Repo, Branch, Into, Message string }
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			origin, err := w.localOrigin(in.Repo)
			if err != nil {
				return nil, err
			}
			if in.Into == "" {
				in.Into = w.defaultBranch(ctx, origin)
			}
			for _, b := range []string{in.Branch, in.Into} {
				if err := checkBranch(b); err != nil {
					return nil, err
				}
			}
			if in.Branch == in.Into {
				return nil, inputErr("a branch cannot be taken into itself")
			}
			unlock := w.lock(in.Repo)
			defer unlock()
			if !w.branchExists(ctx, origin, in.Branch) || !w.branchExists(ctx, origin, in.Into) {
				return nil, inputErr("both branches must exist")
			}
			before, _ := w.bare(ctx, origin, nil, "", "rev-parse", "--short=12", "refs/heads/"+in.Into)
			if _, err := w.bare(ctx, origin, nil, "", "merge-base", "--is-ancestor", "refs/heads/"+in.Branch, "refs/heads/"+in.Into); err == nil {
				return map[string]any{"repo": in.Repo, "merged": false, "reason": "nothing to take: " + in.Into + " has it all"}, nil
			}
			// a fast-forward needs no worktree
			if _, err := w.bare(ctx, origin, nil, "", "merge-base", "--is-ancestor", "refs/heads/"+in.Into, "refs/heads/"+in.Branch); err == nil {
				head, _ := w.bare(ctx, origin, nil, "", "rev-parse", "refs/heads/"+in.Branch)
				if _, err := w.bare(ctx, origin, nil, "", "update-ref", "refs/heads/"+in.Into, strings.TrimSpace(head), strings.TrimSpace(before)); err != nil {
					return nil, err
				}
				return map[string]any{"repo": in.Repo, "merged": true, "fast_forward": true, "into": in.Into,
					"commit": strings.TrimSpace(head)[:12]}, nil
			}
			return w.mergeCommit(ctx, origin, in.Repo, in.Branch, in.Into, in.Message)
		},
	}
}

// mergeCommit merges in a temporary worktree of the origin, removed again whatever happens.
func (w *Workshop) mergeCommit(ctx context.Context, origin, repo, branch, into, message string) (any, error) {
	tmp := filepath.Join(w.dir, ".merge", repo+"-"+strconv.FormatInt(time.Now().UnixNano(), 36))
	if err := os.MkdirAll(filepath.Dir(tmp), 0o700); err != nil {
		return nil, err
	}
	if _, err := w.bare(ctx, origin, nil, "", "worktree", "add", "--quiet", "--", tmp, into); err != nil {
		return nil, err
	}
	defer func() {
		_, _ = w.bare(context.WithoutCancel(ctx), origin, nil, "", "worktree", "remove", "--force", "--", tmp)
		_ = os.RemoveAll(tmp)
		_, _ = w.bare(context.WithoutCancel(ctx), origin, nil, "", "worktree", "prune")
	}()
	msg := strings.TrimSpace(message)
	if msg == "" {
		msg = "Take " + branch + " into " + into
	}
	if _, err := w.git(ctx, tmp, nil, "", "merge", "--no-ff", "--quiet", "-m", msg, "refs/heads/"+branch); err != nil {
		files, _ := w.git(ctx, tmp, nil, "", "diff", "--name-only", "--diff-filter=U")
		_, _ = w.git(ctx, tmp, nil, "", "merge", "--abort")
		conflicts := strings.Fields(files)
		if len(conflicts) > 0 {
			return map[string]any{"repo": repo, "merged": false, "conflicts": conflicts,
				"reason": "the branches change the same lines: nothing was taken"}, nil
		}
		return nil, err
	}
	head, _ := w.git(ctx, tmp, nil, "", "rev-parse", "--short=12", "HEAD")
	return map[string]any{"repo": repo, "merged": true, "fast_forward": false, "into": into, "commit": strings.TrimSpace(head)}, nil
}

// --- workshop.publish / workshop.pull ------------------------------------------------------

// capPublish pushes a branch of a local repository to its remote (the call's Spec): never forced,
// so a remote that moved on is a refusal, not an overwrite.
func (w *Workshop) capPublish() capability.Capability {
	return wcap{
		name: "workshop.publish", manage: true,
		desc: "Push a branch of a local repository to its remote (GitHub or another allowed host); never forced.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo","branch"],"properties":{` + repoProp + `,` +
			`"branch":{"type":"string","maxLength":100}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct{ Repo, Branch string }
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			spec, ok := specOf(ctx)
			if !ok {
				return nil, inputErr("the hub did not say where to publish")
			}
			if err := w.checkRemote(spec); err != nil {
				return nil, err
			}
			origin, err := w.localOrigin(in.Repo)
			if err != nil {
				return nil, err
			}
			if err := checkBranch(in.Branch); err != nil {
				return nil, err
			}
			unlock := w.lock(in.Repo)
			defer unlock()
			if !w.branchExists(ctx, origin, in.Branch) {
				return nil, inputErr("there is no branch %q", in.Branch)
			}
			ref := "refs/heads/" + in.Branch
			if _, err := w.bare(ctx, origin, authFor(spec), "", "push", "--", spec.URL, ref+":"+ref); err != nil {
				return nil, fmt.Errorf("publish failed: %w", err)
			}
			commit, _ := w.bare(ctx, origin, nil, "", "rev-parse", "--short=12", ref)
			return map[string]any{"repo": in.Repo, "published": true, "branch": in.Branch, "commit": strings.TrimSpace(commit)}, nil
		},
	}
}

// capPull brings a branch up to date with its remote: a fast-forward only; a branch that went its
// own way here stays as it is and says so.
func (w *Workshop) capPull() capability.Capability {
	return wcap{
		name: "workshop.pull", manage: true,
		desc: "Bring a branch of a local repository up to date with its remote (fast-forward only).",
		schema: `{"type":"object","additionalProperties":false,"required":["repo","branch"],"properties":{` + repoProp + `,` +
			`"branch":{"type":"string","maxLength":100}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct{ Repo, Branch string }
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			spec, ok := specOf(ctx)
			if !ok {
				return nil, inputErr("the hub did not say where to pull from")
			}
			if err := w.checkRemote(spec); err != nil {
				return nil, err
			}
			origin, err := w.localOrigin(in.Repo)
			if err != nil {
				return nil, err
			}
			if err := checkBranch(in.Branch); err != nil {
				return nil, err
			}
			unlock := w.lock(in.Repo)
			defer unlock()
			if _, err := w.bare(ctx, origin, authFor(spec), "", "fetch", "--quiet", "--no-tags", "--", spec.URL, "refs/heads/"+in.Branch); err != nil {
				return nil, fmt.Errorf("fetch failed: %w", err)
			}
			theirs, err := w.bare(ctx, origin, nil, "", "rev-parse", "FETCH_HEAD")
			if err != nil {
				return nil, err
			}
			theirs = strings.TrimSpace(theirs)
			ref := "refs/heads/" + in.Branch
			if !w.branchExists(ctx, origin, in.Branch) {
				if _, err := w.bare(ctx, origin, nil, "", "update-ref", ref, theirs); err != nil {
					return nil, err
				}
				return map[string]any{"repo": in.Repo, "branch": in.Branch, "updated": true, "commit": theirs[:12]}, nil
			}
			ours, _ := w.bare(ctx, origin, nil, "", "rev-parse", ref)
			ours = strings.TrimSpace(ours)
			if ours == theirs {
				return map[string]any{"repo": in.Repo, "branch": in.Branch, "updated": false, "reason": "already up to date"}, nil
			}
			if _, err := w.bare(ctx, origin, nil, "", "merge-base", "--is-ancestor", ours, theirs); err != nil {
				return map[string]any{"repo": in.Repo, "branch": in.Branch, "updated": false,
					"reason": "the branch has commits the remote does not have: take them over there first"}, nil
			}
			if _, err := w.bare(ctx, origin, nil, "", "update-ref", ref, theirs, ours); err != nil {
				return nil, err
			}
			return map[string]any{"repo": in.Repo, "branch": in.Branch, "updated": true, "commit": theirs[:12]}, nil
		},
	}
}

// --- workshop.remove ---------------------------------------------------------------------------

func (w *Workshop) capRemove() capability.Capability {
	return wcap{
		name: "workshop.remove", manage: true,
		desc: "Delete a local repository and its clone from this workshop (it cannot be undone).",
		schema: `{"type":"object","additionalProperties":false,"required":["repo","confirm"],"properties":{` + repoProp + `,` +
			`"confirm":{"const":true}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Repo    string
				Confirm bool
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			if !in.Confirm {
				return nil, inputErr("removing a repository needs confirm: true")
			}
			origin, err := w.localOrigin(in.Repo)
			if err != nil {
				return nil, err
			}
			unlock := w.lock(in.Repo)
			defer unlock()
			if err := os.RemoveAll(origin); err != nil {
				return nil, err
			}
			if dir, err := w.repoDir(in.Repo); err == nil {
				if _, err := os.Stat(dir); err == nil || !errors.Is(err, fs.ErrNotExist) {
					_ = os.RemoveAll(dir)
				}
			}
			return map[string]any{"repo": in.Repo, "removed": true}, nil
		},
	}
}

// --- repo.commit ---------------------------------------------------------------------------------

// capCommit keeps the change of the worktree on its work branch in the local origin: scratch work
// that stays in this workshop (the default branch is the person's, by workshop.merge). Only for a
// local repository; a remote one is changed by a confirmed repo.push.
func (w *Workshop) capCommit() capability.Capability {
	return wcap{
		name: "repo.commit",
		desc: "Commit the change in the worktree of a local repository and keep it on its work branch `topcoat/<topic>` in the workshop's own repository. Never the default branch; nothing leaves the workshop.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo","message"],"properties":{` + repoProp + `,` +
			`"message":{"type":"string","minLength":3,"maxLength":2000,"description":"the commit message"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct{ Repo, Message string }
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			spec, err := w.specFor(ctx)
			if err != nil {
				return nil, err
			}
			if !spec.Local {
				return nil, inputErr("repo.commit keeps work in a local repository; %q is remote: propose repo.push", in.Repo)
			}
			dir, err := w.clone(in.Repo)
			if err != nil {
				return nil, err
			}
			msg := strings.TrimSpace(in.Message)
			if len(msg) < 3 || len(msg) > maxMessageBytes {
				return nil, inputErr("the commit message needs 3 to %d characters", maxMessageBytes)
			}
			unlock := w.lock(in.Repo)
			defer unlock()
			branch := w.currentBranch(ctx, dir)
			if !strings.HasPrefix(branch, "topcoat/") || branch == spec.Branch {
				return nil, inputErr("the worktree is on %q: work is kept on a branch topcoat/<topic> (repo.start)", branch)
			}
			if _, err := w.git(ctx, dir, nil, "", "add", "-A"); err != nil {
				return nil, err
			}
			diff, err := w.git(ctx, dir, nil, "", "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD")
			if err != nil {
				return nil, err
			}
			if strings.TrimSpace(diff) == "" {
				return nil, inputErr("there is no change to keep")
			}
			if _, n := tools.Mask(diff); n > 0 {
				_, _ = w.git(ctx, dir, nil, "", "reset", "--quiet")
				return nil, inputErr("the change contains %d value(s) that look like secrets: nothing was kept", n)
			}
			if _, err := w.git(ctx, dir, nil, msg+"\n", "commit", "--quiet", "-F", "-"); err != nil {
				return nil, err
			}
			commit, _ := w.git(ctx, dir, nil, "", "rev-parse", "--short=12", "HEAD")
			if _, err := w.git(ctx, dir, nil, "", "push", "--quiet", "origin", "HEAD:refs/heads/"+branch); err != nil {
				return nil, err
			}
			return map[string]any{"repo": in.Repo, "kept": true, "branch": branch, "commit": strings.TrimSpace(commit)}, nil
		},
	}
}

// cutRunes keeps at most n characters of s.
func cutRunes(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}
