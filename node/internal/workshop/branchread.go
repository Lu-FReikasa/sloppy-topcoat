package workshop

import (
	"context"
	"regexp"
	"strconv"
	"strings"
)

// Reads on a large task's branch (D109, D110). The worktree stays on the live code between the
// sub-tasks of a large task; what the sub-tasks before changed lies on the local branch
// topcoat/v-<nr>. repo.read and repo.outline read that branch through git's objects when a call
// names it, never through the worktree, so nothing is checked out for a read. Only a large task's
// branch may be named, and it must exist in this workshop.

var largeBranchRe = regexp.MustCompile(`^topcoat/v-[a-z0-9][a-z0-9-]{0,39}$`)

const branchProp = `"branch":{"type":"string","maxLength":60,"description":"read a large task's local branch topcoat/v-<nr> instead of the worktree"}`

// largeTopic says whether a topic of repo.start names a large task's branch (v-<nr>).
func largeTopic(slug string) bool { return largeBranchRe.MatchString("topcoat/" + slug) }

// atBranch checks a branch named by a read and returns its full ref.
func (w *Workshop) atBranch(ctx context.Context, dir, branch string) (string, error) {
	if !largeBranchRe.MatchString(branch) {
		return "", inputErr("branch: only a large task's branch topcoat/v-<nr> can be read")
	}
	ref := "refs/heads/" + branch
	if _, err := w.git(ctx, dir, nil, "", "rev-parse", "--verify", "--quiet", ref+"^{commit}"); err != nil {
		return "", inputErr("there is no branch %s in this workshop", branch)
	}
	return ref, nil
}

type treeBlob struct {
	mode, kind, sha, path string
	size                  int64
}

// treeBlobs lists the entries below prefix ("" = all) at ref, recursively, with their sizes.
func (w *Workshop) treeBlobs(ctx context.Context, dir, ref, prefix string) ([]treeBlob, error) {
	args := []string{"ls-tree", "-r", "-z", "--long", ref}
	if prefix != "" {
		// literal: a path from a call is a name, never a pattern or other pathspec magic
		args = append(args, "--", ":(literal)"+prefix)
	}
	out, err := w.git(ctx, dir, nil, "", args...)
	if err != nil {
		return nil, err
	}
	var blobs []treeBlob
	for _, line := range strings.Split(out, "\x00") {
		head, p, ok := strings.Cut(line, "\t")
		if !ok {
			continue
		}
		f := strings.Fields(head) // mode type object size
		if len(f) != 4 {
			continue
		}
		size, _ := strconv.ParseInt(f[3], 10, 64) // "-" for a submodule: 0
		blobs = append(blobs, treeBlob{mode: f[0], kind: f[1], sha: f[2], path: p, size: size})
	}
	return blobs, nil
}

// readableBlob says whether an entry is a file the workshop reads: a plain blob (no link, no
// submodule), not under .git, not a file that may hold credentials.
func readableBlob(b treeBlob) bool {
	if b.kind != "blob" || (b.mode != "100644" && b.mode != "100755") || secretName(b.path) {
		return false
	}
	for _, seg := range strings.Split(b.path, "/") {
		if strings.EqualFold(seg, ".git") {
			return false
		}
	}
	return true
}

func (w *Workshop) blobBytes(ctx context.Context, dir string, b treeBlob) ([]byte, error) {
	out, err := w.git(ctx, dir, nil, "", "cat-file", "blob", b.sha)
	if err != nil {
		return nil, err
	}
	return []byte(out), nil
}

// readAt returns the clean path and the text of one file at ref, with the checks of repo.read.
func (w *Workshop) readAt(ctx context.Context, dir, ref, p string) (string, string, error) {
	rel, err := cleanRel(p)
	if err != nil {
		return "", "", err
	}
	if secretName(rel) {
		return "", "", inputErr("%s may hold credentials: the workshop does not touch it", rel)
	}
	blobs, err := w.treeBlobs(ctx, dir, ref, rel)
	if err != nil {
		return "", "", err
	}
	for _, b := range blobs {
		if b.path != rel {
			continue
		}
		if !readableBlob(b) {
			return "", "", inputErr("%s is not a file of this repository", rel)
		}
		if b.size > maxFileBytes {
			return "", "", inputErr("%s is %d bytes: too large to read here (search it or read another file)", rel, b.size)
		}
		data, err := w.blobBytes(ctx, dir, b)
		if err != nil {
			return "", "", inputErr("%s cannot be read", rel)
		}
		text, err := asText(rel, data)
		return rel, text, err
	}
	return "", "", inputErr("%s is not a file of this repository", rel)
}
