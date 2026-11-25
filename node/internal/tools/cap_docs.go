package tools

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"

	"topcoat/node/internal/providers/capability"
)

// DefaultDocsExclude is the part of the exclude list that cannot be switched off (E2-03): a path
// with a segment — a directory or a file name — that matches one of these globs (case
// insensitive) is not listed, not read, not counted by name and not reachable through a link.
// config.tools.docs_exclude adds to it, never replaces it. The hub keeps the same list.
var DefaultDocsExclude = []string{
	"*SENSIBEL*", "*ZUGANGSDATEN*", "*.key", "*.pem", "*.env", ".env*", "*password*", "*passwort*",
	"*secret*", "*credential*", "*.kdbx", "id_rsa*", "id_ed25519*", ".git",
}

// docsExtensions are the file types the knowledge base indexes.
var docsExtensions = map[string]bool{".md": true, ".markdown": true, ".txt": true}

const (
	// DocsReadBytes is the most of a file one docs.read returns: the output limit of a call is
	// 64 KiB and JSON escaping can double a text, so a page stays well below half of it.
	DocsReadBytes = 24 << 10
	// MaxDocsList is the most entries one docs.list returns; more are fetched with `after`.
	MaxDocsList      = 1500
	defaultDocsList  = 500
	maxDocsWalk      = 100000 // entries looked at in one listing
	maxDocsFileBytes = 1 << 20
)

// DocsExcluded reports whether a relative path matches the exclude list (the default list plus
// extra globs) in any of its segments.
func DocsExcluded(rel string, extra []string) bool {
	segments := strings.FieldsFunc(rel, func(r rune) bool { return r == '/' || r == '\\' })
	for _, s := range segments {
		s = strings.ToLower(s)
		for _, pattern := range append(append([]string{}, DefaultDocsExclude...), extra...) {
			if ok, _ := path.Match(strings.ToLower(pattern), s); ok {
				return true
			}
		}
	}
	return false
}

// ValidDocsGlob reports whether g is a usable exclude glob (used by the config check).
func ValidDocsGlob(g string) bool {
	_, err := path.Match(g, "x")
	return err == nil && strings.TrimSpace(g) != ""
}

var errDocsNotAvailable = inputErr("that file is not available")

// docsRoot picks the released root the caller means: exactly one of DocsRoots (an absolute path
// as configured), or the only one when a single root is released. Returns its resolved path.
func (o Options) docsRoot(want string) (string, error) {
	if len(o.DocsRoots) == 0 {
		return "", inputErr("no directories are released for the knowledge base: config.tools.docs_roots is empty (the default is deny; see INSTALL-NODE)")
	}
	chosen := ""
	switch {
	case want == "" && len(o.DocsRoots) == 1:
		chosen = o.DocsRoots[0]
	case want == "":
		return "", inputErr("root is required: this node releases %d directories", len(o.DocsRoots))
	default:
		for _, r := range o.DocsRoots {
			if filepath.Clean(r) == filepath.Clean(want) {
				chosen = r
			}
		}
		if chosen == "" {
			return "", inputErr("that directory is not released (config.tools.docs_roots)")
		}
	}
	real, err := filepath.EvalSymlinks(chosen)
	if err != nil {
		return "", inputErr("the released directory cannot be read: %v", err)
	}
	return real, nil
}

func docsListCap(o Options) capability.Capability {
	return fn{
		name: "docs.list",
		desc: "List the Markdown and text files (.md, .markdown, .txt) of a directory released for the knowledge base, sorted by path, with size and modification time. Files and folders whose name is on the exclude list (credentials, keys, *SENSIBEL* …) are never listed. Page with `after`.",
		schema: `{"type":"object","additionalProperties":false,"properties":{` +
			`"root":{"type":"string","maxLength":512,"description":"one of the released directories (optional when only one is released)"},` +
			`"limit":{"type":"integer","minimum":1,"maximum":1500,"description":"entries per call (default 500)"},` +
			`"after":{"type":"string","maxLength":1024,"description":"list only paths after this one (the previous call's next_after)"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Root  string `json:"root"`
				Limit int    `json:"limit"`
				After string `json:"after"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			limit := in.Limit
			if limit == 0 {
				limit = defaultDocsList
			}
			if limit < 1 || limit > MaxDocsList {
				return nil, inputErr("limit must be 1..%d", MaxDocsList)
			}
			root, err := o.docsRoot(in.Root)
			if err != nil {
				return nil, err
			}
			type entry struct {
				Path  string `json:"path"`
				Size  int64  `json:"size"`
				Mtime int64  `json:"mtime"`
			}
			var files []entry
			excluded, seen := 0, 0
			limited := false
			err = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
				if err != nil {
					return nil // an unreadable entry is skipped, not an error for the whole listing
				}
				if ctx.Err() != nil {
					return ctx.Err()
				}
				if seen++; seen > maxDocsWalk {
					limited = true
					return filepath.SkipAll
				}
				if p == root {
					return nil
				}
				rel, rerr := filepath.Rel(root, p)
				if rerr != nil {
					return nil
				}
				if DocsExcluded(rel, o.DocsExclude) {
					excluded++
					if d.IsDir() {
						return filepath.SkipDir
					}
					return nil
				}
				if d.Type()&fs.ModeSymlink != 0 || d.IsDir() || !d.Type().IsRegular() {
					return nil // links are never followed: a link could lead to anything
				}
				if !docsExtensions[strings.ToLower(filepath.Ext(d.Name()))] {
					return nil
				}
				info, ierr := d.Info()
				if ierr != nil {
					return nil
				}
				files = append(files, entry{filepath.ToSlash(rel), info.Size(), info.ModTime().Unix()})
				return nil
			})
			if err != nil && !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded) {
				return nil, err
			}
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			sort.Slice(files, func(i, j int) bool { return files[i].Path < files[j].Path })
			start := 0
			if in.After != "" {
				start = sort.Search(len(files), func(i int) bool { return files[i].Path > in.After })
			}
			page := files[start:]
			out := map[string]any{"total": len(files), "excluded": excluded, "walk_limited": limited}
			if len(page) > limit {
				page = page[:limit]
				out["truncated"], out["next_after"] = true, page[len(page)-1].Path
			} else {
				out["truncated"] = false
			}
			if page == nil {
				page = []entry{}
			}
			out["files"] = page
			return out, nil
		},
	}
}

func docsReadCap(o Options) capability.Capability {
	return fn{
		name: "docs.read",
		desc: "Read one Markdown or text file of a directory released for the knowledge base, a page of at most 24 KiB at a time (`offset`, then `next_offset`), secrets masked. A file on the exclude list is refused like a file that does not exist.",
		schema: `{"type":"object","additionalProperties":false,"required":["path"],"properties":{` +
			`"root":{"type":"string","maxLength":512,"description":"one of the released directories (optional when only one is released)"},` +
			`"path":{"type":"string","minLength":1,"maxLength":1024,"description":"path relative to the root, with / as separator (from docs.list)"},` +
			`"offset":{"type":"integer","minimum":0,"description":"byte offset to start at (the previous call's next_offset)"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Root   string `json:"root"`
				Path   string `json:"path"`
				Offset int64  `json:"offset"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if in.Offset < 0 {
				return nil, inputErr("offset must be >= 0")
			}
			root, err := o.docsRoot(in.Root)
			if err != nil {
				return nil, err
			}
			rel := in.Path
			if strings.ContainsRune(rel, 0) || strings.HasPrefix(rel, "/") || strings.Contains(rel, "\\") ||
				filepath.IsAbs(filepath.FromSlash(rel)) {
				return nil, errDocsNotAvailable
			}
			for _, seg := range strings.Split(rel, "/") {
				if seg == "" || seg == "." || seg == ".." {
					return nil, errDocsNotAvailable
				}
			}
			if !docsExtensions[strings.ToLower(filepath.Ext(rel))] || DocsExcluded(rel, o.DocsExclude) {
				return nil, errDocsNotAvailable
			}
			real, err := filepath.EvalSymlinks(filepath.Join(root, filepath.FromSlash(rel)))
			if err != nil || !within(real, root) {
				return nil, errDocsNotAvailable
			}
			// a link inside the root may point at a file the list forbids
			if r2, err := filepath.Rel(root, real); err != nil || DocsExcluded(r2, o.DocsExclude) {
				return nil, errDocsNotAvailable
			}
			f, err := os.Open(real) //nolint:gosec // real is inside a released root, not excluded, and was resolved through links
			if err != nil {
				return nil, errDocsNotAvailable
			}
			defer f.Close() //nolint:errcheck // read-only
			st, err := f.Stat()
			if err != nil || !st.Mode().IsRegular() {
				return nil, errDocsNotAvailable
			}
			if st.Size() > maxDocsFileBytes {
				return nil, inputErr("the file is larger than %d bytes and is not indexed", maxDocsFileBytes)
			}
			offset := in.Offset
			if offset > st.Size() {
				offset = st.Size()
			}
			if _, err := f.Seek(offset, io.SeekStart); err != nil {
				return nil, err
			}
			buf, err := io.ReadAll(io.LimitReader(f, DocsReadBytes+utf8.UTFMax))
			if err != nil {
				return nil, err
			}
			// start on a character, end on a line (or at least a character): a page never splits
			// a rune, and usually not a line, so masking sees whole lines
			skip := 0
			for skip < len(buf) && skip < utf8.UTFMax && !utf8.RuneStart(buf[skip]) {
				skip++
			}
			buf = buf[skip:]
			offset += int64(skip)
			more := offset+int64(len(buf)) < st.Size()
			if len(buf) > DocsReadBytes {
				buf = buf[:DocsReadBytes]
				more = true
			}
			if more {
				if i := strings.LastIndexByte(string(buf), '\n'); i > len(buf)/2 {
					buf = buf[:i+1]
				}
				for len(buf) > 0 && !utf8.Valid(buf) {
					buf = buf[:len(buf)-1]
				}
			}
			text, masked := Mask(string(buf))
			out := map[string]any{
				"path": rel, "offset": offset, "size": st.Size(), "mtime": st.ModTime().Unix(),
				"content": text, "masked": masked,
			}
			if more {
				out["next_offset"] = offset + int64(len(buf))
			} else {
				out["next_offset"] = nil
			}
			return out, nil
		},
	}
}
