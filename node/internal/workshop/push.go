package workshop

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/url"
	"strings"
	"unicode/utf8"

	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/tools"
)

const maxMessageBytes = 2000

func diffHash(diff string) string {
	h := sha256.Sum256([]byte(diff))
	return hex.EncodeToString(h[:])
}

// capPush is the one step that leaves the workshop (D52). The hub runs it only for a proposal the
// user confirmed with a click; the proposal shows the diff, and `diff_sha256` ties the click to
// exactly that change: if the worktree differs by one byte, nothing is committed.
//
// It pushes a branch `topcoat/*` that is not the default branch, never forces, and refuses a
// change in which the secret scanner finds something.
func (w *Workshop) capPush() capability.Capability {
	return wcap{
		name: "repo.push", confirm: true,
		desc: "Commit the change in the worktree and push it to the branch `topcoat/<topic>` on the remote. Needs a confirmed proposal; never forces; never pushes the default branch.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo","message","branch","diff_sha256"],"properties":{` + repoProp + `,` +
			`"message":{"type":"string","minLength":3,"maxLength":2000,"description":"the commit message"},` +
			`"branch":{"type":"string","maxLength":100,"description":"the work branch, topcoat/<topic>"},` +
			`"diff_sha256":{"type":"string","pattern":"^[0-9a-f]{64}$","description":"hash of the diff the user saw"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Repo       string `json:"repo"`
				Message    string `json:"message"`
				Branch     string `json:"branch"`
				DiffSHA256 string `json:"diff_sha256"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			spec, err := w.specFor(ctx)
			if err != nil {
				return nil, err
			}
			dir, err := w.clone(in.Repo)
			if err != nil {
				return nil, err
			}
			msg := strings.TrimSpace(in.Message)
			if len(msg) < 3 || len(msg) > maxMessageBytes || !utf8.ValidString(msg) || strings.ContainsRune(msg, 0) {
				return nil, inputErr("the commit message needs 3 to %d characters of text", maxMessageBytes)
			}
			if !strings.HasPrefix(in.Branch, "topcoat/") || in.Branch == spec.Branch || !branchRe.MatchString(in.Branch) ||
				strings.Contains(in.Branch, "..") || strings.HasSuffix(in.Branch, "/") || strings.HasSuffix(in.Branch, ".lock") {
				return nil, inputErr("only a branch topcoat/<topic> is pushed, never the default branch")
			}
			unlock := w.lock(in.Repo)
			defer unlock()
			if cur := w.currentBranch(ctx, dir); cur != in.Branch {
				return nil, inputErr("the worktree is on %q, not on %q: nothing was pushed", cur, in.Branch)
			}
			if _, err := w.git(ctx, dir, nil, "", "add", "-A"); err != nil {
				return nil, err
			}
			diff, err := w.git(ctx, dir, nil, "", "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD")
			if err != nil {
				return nil, err
			}
			if strings.TrimSpace(diff) == "" {
				return nil, inputErr("there is no change to push")
			}
			if diffHash(diff) != in.DiffSHA256 {
				return nil, inputErr("the worktree changed since the diff was shown: nothing was pushed, review it again")
			}
			if _, n := tools.Mask(diff); n > 0 {
				return nil, inputErr("the change contains %d value(s) that look like secrets: nothing was pushed", n)
			}
			if _, err := w.git(ctx, dir, nil, msg+"\n", "commit", "--quiet", "-F", "-"); err != nil {
				return nil, err
			}
			commit, _ := w.git(ctx, dir, nil, "", "rev-parse", "--short=12", "HEAD")
			// no --force, no +refspec: a branch that moved on the remote is a refusal, not an overwrite
			auth := authFor(spec)
			if spec.Local {
				auth = nil // the origin is in this workshop
			}
			if _, err := w.git(ctx, dir, auth, "", "push", "origin", "HEAD:refs/heads/"+in.Branch); err != nil {
				return nil, err
			}
			res := map[string]any{"repo": in.Repo, "pushed": true, "branch": in.Branch, "commit": strings.TrimSpace(commit)}
			if u, err := url.Parse(spec.URL); err == nil && u.Scheme == "https" {
				res["compare_url"] = strings.TrimSuffix(spec.URL, ".git") + "/compare/" + spec.Branch + "..." + in.Branch
			}
			return res, nil
		},
	}
}
