package workshop

// repo.outline (plan step 1.11, D119): a map of the repository for the start of an investigation.
// Per file its size and its named places, found by the same rules as the hub's code_symbols.py
// (which works on the text of one repo.read). Differences, so the node needs no parser:
//   - Python by its def/class lines and their indentation (code_symbols.py parses with ast):
//     classes, functions, methods and module-level assignments; class-level assignments are left out.
//   - Go is added: functions, methods (Receiver.name) and types.
//   - Markdown headings inside a fenced code block are not headings.
//   - No `imports` symbol: a map does not need it.
// Pure functions, no I/O, except the capability at the end.

import (
	"context"
	"encoding/json"
	"os"
	"regexp"
	"slices"
	"sort"
	"strings"
	"unicode/utf8"

	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/tools"
)

const (
	outlineBytes      = 40 << 10 // of one answer (the node's output limit is 64 KiB)
	outlineFiles      = 50       // files per page by default
	outlineSymbols    = 40       // symbols per file by default
	outlineMaxSymbols = 200
	outlineName       = 120 // characters of one name (a long selector list, a long heading)
	// outlineNameBytes bounds a nested name while it is built (a.b.c…), so deep nesting cannot
	// grow the names quadratically; the answer cuts them to outlineName after masking.
	outlineNameBytes = 1024
	// outlineScan: the search for the ends in one file reads its bytes at most this many times in all
	// (once per declaration around a line: Topcoat's own deepest JSON schemas need 5); see scanBudget.
	outlineScan = 32
)

// outlineKinds are the file kinds repo.outline knows, by the short names of code_symbols.py.
var outlineKinds = []string{"py", "ts", "go", "json", "md", "css"}

type outlineSymbol struct {
	Name  string `json:"name"`
	Kind  string `json:"kind"`  // class, function, method, const, type, interface, enum, component, key, heading, rule
	Start int    `json:"start"` // first line, 1-based (a decorator or doc comment above belongs to it)
	End   int    `json:"end"`   // last line, 1-based, inclusive
	depth int    // nesting: 0 for a top-level name
}

func outlineKind(p string) string {
	name := strings.ToLower(p[strings.LastIndex(p, "/")+1:])
	for _, k := range []struct {
		kind string
		exts []string
	}{
		{"py", []string{".py"}},
		{"ts", []string{".ts", ".tsx", ".js", ".jsx", ".mjs"}},
		{"go", []string{".go"}},
		{"json", []string{".json"}},
		{"md", []string{".md"}},
		{"css", []string{".css"}},
	} {
		for _, e := range k.exts {
			if strings.HasSuffix(name, e) {
				return k.kind
			}
		}
	}
	return ""
}

// splitLines splits a text into its lines (LF or CRLF), without an empty last one.
func splitLines(text string) []string {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	text = strings.TrimSuffix(text, "\n")
	if text == "" {
		return nil
	}
	return strings.Split(text, "\n")
}

func indentOf(line string) int { return len(line) - len(strings.TrimLeft(line, " \t")) }

// scanBudget bounds the search for the ends in one file. Each declaration looks for its end from its
// own line (braces, or indentation for Python and JSON), so a file whose declarations never close
// (`f(` on every line) would make every one of them scan to the end of the file: quadratic work that
// a hostile file could use to keep the node busy for minutes per call. Past the budget a declaration
// ends at the line where its scan stopped.
type scanBudget struct{ left int }

func newScanBudget(lines []string) *scanBudget {
	n := 0
	for _, l := range lines {
		n += len(l) + 1
	}
	return &scanBudget{left: outlineScan*n + 64<<10}
}

// take spends n units and says whether there were any left.
func (b *scanBudget) take(n int) bool {
	b.left -= n
	return b.left >= 0
}

// joinName is parent.name, cut at outlineNameBytes (on a rune boundary).
func joinName(parent, name string) string {
	s := name
	if parent != "" {
		s = parent + "." + name
	}
	if len(s) <= outlineNameBytes {
		return s
	}
	cut := outlineNameBytes
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut] + "…"
}

// symbolsOf returns the named places of a file in the order they stand; 0-based start, exclusive
// end internally, converted to 1-based inclusive lines at the end.
func symbolsOf(p, text string) []outlineSymbol {
	lines := splitLines(text)
	var out []outlineSymbol
	switch outlineKind(p) {
	case "py":
		out = pySymbols(lines)
	case "ts":
		out = tsSymbols(lines)
	case "go":
		out = goSymbols(lines)
	case "json":
		out = jsonSymbols(lines)
	case "md":
		out = mdSymbols(lines)
	case "css":
		out = cssSymbols(lines)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Start != out[j].Start {
			return out[i].Start < out[j].Start
		}
		return out[i].End > out[j].End
	})
	for i := range out {
		out[i].Start++ // 0-based start -> 1-based; the exclusive 0-based end is the 1-based last line
		if out[i].End < out[i].Start {
			out[i].End = out[i].Start
		}
	}
	return out
}

// --- Python -------------------------------------------------------------------------------------

var (
	pyDef    = regexp.MustCompile(`^(\s*)(?:async\s+)?(def|class)\s+(\w+)`)
	pyAssign = regexp.MustCompile(`^([A-Za-z_]\w*)\s*(?::[^=]*)?=(?:[^=]|$)`)
)

// pyInString marks the lines that start inside a triple-quoted string (a docstring's text at
// column 0 is no statement).
func pyInString(lines []string) []bool {
	in := make([]bool, len(lines))
	quote := ""
	for i, line := range lines {
		in[i] = quote != ""
		for j := 0; j+3 <= len(line); {
			t := line[j : j+3]
			switch {
			case quote == "" && (t == `"""` || t == `'''`):
				quote = t
				j += 3
			case quote != "" && t == quote:
				quote = ""
				j += 3
			default:
				j++
			}
		}
	}
	return in
}

// pyEnd is one past the last line of the statement at i with indentation indent: the next code
// line at that indentation or less (comments, blank lines, string lines and closing brackets of
// a long signature do not end it), trailing blank and comment lines left out.
func pyEnd(lines []string, inStr []bool, i, indent int, b *scanBudget) int {
	end := len(lines)
	for j := i + 1; j < len(lines); j++ {
		if !b.take(len(lines[j]) + 1) {
			end = j
			break
		}
		s := strings.TrimSpace(lines[j])
		if inStr[j] || s == "" || strings.HasPrefix(s, "#") || strings.ContainsAny(s[:1], ")]}") {
			continue
		}
		if indentOf(lines[j]) <= indent {
			end = j
			break
		}
	}
	for end > i+1 && (strings.TrimSpace(lines[end-1]) == "" || strings.HasPrefix(strings.TrimSpace(lines[end-1]), "#")) {
		end--
	}
	return end
}

func pySymbols(lines []string) []outlineSymbol {
	inStr := pyInString(lines)
	b := newScanBudget(lines)
	type open struct {
		indent int
		name   string // the full name (Outer.inner)
		class  bool
	}
	var out []outlineSymbol
	var stack []open
	for i, line := range lines {
		s := strings.TrimSpace(line)
		// a closing bracket of a long signature does not close the def it belongs to
		if inStr[i] || s == "" || strings.HasPrefix(s, "#") || strings.ContainsAny(s[:1], ")]}") {
			continue
		}
		indent := indentOf(line)
		for len(stack) > 0 && stack[len(stack)-1].indent >= indent {
			stack = stack[:len(stack)-1]
		}
		if m := pyDef.FindStringSubmatch(line); m != nil {
			parent := ""
			if len(stack) > 0 {
				parent = stack[len(stack)-1].name
			}
			full := joinName(parent, m[3])
			kind := "function"
			if m[2] == "class" {
				kind = "class"
			} else if len(stack) > 0 && stack[len(stack)-1].class {
				kind = "method"
			}
			start := i
			for start > 0 && strings.HasPrefix(strings.TrimSpace(lines[start-1]), "@") && indentOf(lines[start-1]) == indent {
				start--
			}
			out = append(out, outlineSymbol{full, kind, start, pyEnd(lines, inStr, i, indent, b), len(stack)})
			stack = append(stack, open{indent, full, m[2] == "class"})
			continue
		}
		if indent == 0 {
			if m := pyAssign.FindStringSubmatch(line); m != nil {
				out = append(out, outlineSymbol{m[1], "const", i, pyEnd(lines, inStr, i, 0, b), 0})
			}
		}
	}
	return out
}

// --- TS / TSX / JS (and the brace counting Go and CSS share) --------------------------------------

var (
	tsDecl = regexp.MustCompile(`^(\s*)(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?` +
		`(function\*?|class|interface|type|enum|const|let|var)\s+(\w+)`)
	tsMethod = regexp.MustCompile(`^(\s+)(?:public\s+|private\s+|protected\s+|static\s+|readonly\s+|async\s+|get\s+|set\s+)*` +
		`(\w+)\s*(?:<[^>]*>)?\s*\([^;]*$`)
	tsNotMethods = map[string]bool{"if": true, "for": true, "while": true, "switch": true, "catch": true,
		"return": true, "function": true, "await": true, "new": true}
	// a line ending so continues its statement on the next line
	goesOn = []string{"=", "=>", "(", ",", "?", ":", "&&", "||", "|", "+", "-", "*", "."}
)

// braceEnd is one past the last line of the declaration at start: its braces closed, or, for one
// without a block (`const x = 1;`, `type A = B`), its statement ended.
func braceEnd(lines []string, start int, b *scanBudget) int {
	depth := 0
	opened := false
	quote := byte(0) // a template or raw string (`) goes on over lines; other quotes end with theirs
	for i := start; i < len(lines); i++ {
		line := lines[i]
		if !b.take(len(line) + 1) {
			return i + 1
		}
	scan:
		for j := 0; j < len(line); j++ {
			c := line[j]
			switch {
			case quote != 0:
				if c == '\\' && quote != '`' {
					j++
				} else if c == quote {
					quote = 0
				}
			case c == '"' || c == '\'' || c == '`':
				quote = c
			case strings.HasPrefix(line[j:], "//"):
				break scan
			case c == '{' || c == '(' || c == '[':
				depth++
				opened = opened || c == '{'
			case c == '}' || c == ')' || c == ']':
				depth--
			}
		}
		if quote == '`' {
			continue
		}
		quote = 0
		if depth > 0 {
			continue
		}
		if opened {
			return i + 1
		}
		tail := strings.TrimRight(strings.SplitN(line, "//", 2)[0], " \t")
		if tail != "" && !hasAnySuffix(tail, goesOn) {
			return i + 1
		}
	}
	return len(lines)
}

func hasAnySuffix(s string, suffixes []string) bool {
	for _, x := range suffixes {
		if strings.HasSuffix(s, x) {
			return true
		}
	}
	return false
}

// docStart: a `/** … */` comment, `//` lines (Go) or decorators right above a declaration belong to it.
func docStart(lines []string, start int, slashes bool) int {
	i := start
	for i > 0 {
		above := strings.TrimSpace(lines[i-1])
		if strings.HasSuffix(above, "*/") || strings.HasPrefix(above, "*") || strings.HasPrefix(above, "/**") ||
			strings.HasPrefix(above, "@") || (slashes && strings.HasPrefix(above, "//")) {
			i--
			continue
		}
		break
	}
	return i
}

func tsSymbols(lines []string) []outlineSymbol {
	type owner struct {
		start, end, depth int
		name              string
		class             bool
	}
	var out []outlineSymbol
	var owners []owner
	named := map[string]bool{}
	b := newScanBudget(lines)
	for i, line := range lines {
		m := tsDecl.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		indent, word, name := len(m[1]), m[2], m[3]
		end := braceEnd(lines, i, b)
		full, depth := name, 0
		if indent > 0 {
			for k := len(owners) - 1; k >= 0 && b.take(1); k-- {
				if o := owners[k]; o.start < i && i < o.end {
					full, depth = joinName(o.name, name), o.depth+1
					break
				}
			}
		}
		var kind string
		switch {
		case strings.HasPrefix(word, "function"):
			kind = "function"
			if isUpper(name) {
				kind = "component"
			}
		case word == "const" || word == "let" || word == "var":
			arrow := strings.Contains(strings.Join(lines[i:min(i+3, end)], " "), "=>")
			switch {
			case arrow && isUpper(name):
				kind = "component"
			case arrow:
				kind = "function"
			default:
				kind = "const"
			}
		default:
			kind = word
		}
		out = append(out, outlineSymbol{full, kind, docStart(lines, i, false), end, depth})
		named[full] = true
		if kind == "class" || kind == "component" || (kind == "function" && indent == 0) {
			owners = append(owners, owner{i, end, depth, full, kind == "class"})
		}
	}
	// methods of classes, at the indentation of the class body
	for _, o := range owners {
		if !o.class {
			continue
		}
		body := -1
		for i := o.start + 1; i < o.end && b.take(len(lines[i])+1); i++ {
			s := strings.TrimSpace(lines[i])
			if s == "" || strings.HasPrefix(s, "//") || strings.HasPrefix(s, "*") || strings.HasPrefix(s, "/*") || strings.HasPrefix(s, "@") {
				continue
			}
			if body < 0 {
				body = indentOf(lines[i])
			}
			m := tsMethod.FindStringSubmatch(lines[i])
			if m == nil || len(m[1]) != body || tsNotMethods[m[2]] {
				continue
			}
			if t := strings.TrimRight(lines[i], " \t"); !strings.HasSuffix(t, "{") && !strings.HasSuffix(t, "(") {
				continue
			}
			name := joinName(o.name, m[2])
			if !named[name] {
				named[name] = true
				out = append(out, outlineSymbol{name, "method", docStart(lines, i, false), braceEnd(lines, i, b), o.depth + 1})
			}
		}
	}
	return out
}

func isUpper(name string) bool {
	return name != "" && name[0] >= 'A' && name[0] <= 'Z'
}

// --- Go -----------------------------------------------------------------------------------------

var (
	goFunc = regexp.MustCompile(`^func\s+(?:\(\s*(?:\w+\s+)?\*?\s*(\w+)(?:\[[^\]]*\])?\s*\)\s*)?(\w+)`)
	goType = regexp.MustCompile(`^type\s+(\w+)(?:\[[^\]]*\])?\s+(interface\b)?`)
)

func goSymbols(lines []string) []outlineSymbol {
	var out []outlineSymbol
	b := newScanBudget(lines)
	for i, line := range lines {
		if m := goFunc.FindStringSubmatch(line); m != nil {
			if m[1] != "" {
				out = append(out, outlineSymbol{m[1] + "." + m[2], "method", docStart(lines, i, true), braceEnd(lines, i, b), 1})
			} else {
				out = append(out, outlineSymbol{m[2], "function", docStart(lines, i, true), braceEnd(lines, i, b), 0})
			}
		} else if m := goType.FindStringSubmatch(line); m != nil {
			kind := "type"
			if m[2] != "" {
				kind = "interface"
			}
			out = append(out, outlineSymbol{m[1], kind, docStart(lines, i, true), braceEnd(lines, i, b), 0})
		}
	}
	return out
}

// --- JSON, Markdown, CSS ------------------------------------------------------------------------

var jsonKey = regexp.MustCompile(`^(\s*)"((?:[^"\\]|\\.)*)"\s*:`)

// jsonSymbols: keys by their lines and indentation (the i18n files are written one key a line).
func jsonSymbols(lines []string) []outlineSymbol {
	type open struct {
		indent int
		key    string // the full key path (a.b.c)
	}
	var out []outlineSymbol
	var stack []open
	b := newScanBudget(lines)
	for i, line := range lines {
		m := jsonKey.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		indent := len(m[1])
		for len(stack) > 0 && stack[len(stack)-1].indent >= indent {
			stack = stack[:len(stack)-1]
		}
		parent := ""
		if len(stack) > 0 {
			parent = stack[len(stack)-1].key
		}
		full := joinName(parent, m[2])
		end := i + 1
		depth := len(stack)
		if t := strings.TrimRight(line, " \t"); strings.HasSuffix(t, "{") || strings.HasSuffix(t, "[") {
			end = len(lines)
			for j := i + 1; j < len(lines); j++ {
				if !b.take(len(lines[j]) + 1) {
					end = j
					break
				}
				if strings.TrimSpace(lines[j]) != "" && indentOf(lines[j]) <= indent {
					end = j + 1
					break
				}
			}
			stack = append(stack, open{indent, full})
		}
		out = append(out, outlineSymbol{full, "key", i, end, depth})
	}
	return out
}

var mdHeading = regexp.MustCompile(`^(#{1,6})\s+(.+?)\s*#*\s*$`)

func mdSymbols(lines []string) []outlineSymbol {
	type head struct{ line, level int }
	var heads []head
	var titles []string
	fence := ""
	for i, line := range lines {
		s := strings.TrimSpace(line)
		if strings.HasPrefix(s, "```") || strings.HasPrefix(s, "~~~") {
			switch {
			case fence == "":
				fence = s[:3]
			case strings.HasPrefix(s, fence):
				fence = ""
			}
			continue
		}
		if fence != "" {
			continue
		}
		if m := mdHeading.FindStringSubmatch(line); m != nil {
			heads = append(heads, head{i, len(m[1])})
			titles = append(titles, m[1]+" "+m[2])
		}
	}
	out := make([]outlineSymbol, 0, len(heads))
	for n, h := range heads {
		end := len(lines)
		for _, next := range heads[n+1:] {
			if next.level <= h.level {
				end = next.line
				break
			}
		}
		out = append(out, outlineSymbol{titles[n], "heading", h.line, end, h.level - 1})
	}
	return out
}

var cssRule = regexp.MustCompile(`^([^\s{}/][^{}]*?)\s*\{`)

func cssSymbols(lines []string) []outlineSymbol {
	var out []outlineSymbol
	b := newScanBudget(lines)
	for i, line := range lines {
		if m := cssRule.FindStringSubmatch(line); m != nil {
			out = append(out, outlineSymbol{strings.Join(strings.Fields(m[1]), " "), "rule", i, braceEnd(lines, i, b), 0})
		}
	}
	return out
}

// keepSymbols keeps at most max symbols, the outer ones first (a class before its methods, a
// top-level key before its children), in the order they stand; it returns how many were left out.
func keepSymbols(syms []outlineSymbol, max int) ([]outlineSymbol, int) {
	if len(syms) <= max {
		return syms, 0
	}
	idx := make([]int, len(syms))
	for i := range idx {
		idx[i] = i
	}
	sort.SliceStable(idx, func(a, b int) bool { return syms[idx[a]].depth < syms[idx[b]].depth })
	idx = idx[:max]
	sort.Ints(idx)
	kept := make([]outlineSymbol, 0, max)
	for _, i := range idx {
		kept = append(kept, syms[i])
	}
	return kept, len(syms) - max
}

func shortName(s string) string {
	if utf8.RuneCountInString(s) <= outlineName {
		return s
	}
	r := []rune(s)
	return string(r[:outlineName-1]) + "…"
}

// --- the capability -----------------------------------------------------------------------------

type outlineFile struct {
	Path    string          `json:"path"`
	Kind    string          `json:"kind"`
	Lines   int             `json:"lines"`
	Bytes   int64           `json:"bytes"`
	Symbols []outlineSymbol `json:"symbols"`
	Left    int             `json:"symbols_left,omitempty"`
	Skipped string          `json:"skipped,omitempty"`
}

func (w *Workshop) capOutline() capability.Capability {
	kinds, _ := json.Marshal(outlineKinds)
	return wcap{
		name: "repo.outline", readOnly: true,
		desc: "A map of the worktree: per source file its size and its named places with their lines " +
			"(Python classes, functions, methods and module constants; TS/JS classes, functions, components, types, consts; " +
			"Go functions, methods and types; JSON keys; Markdown headings; CSS rules). start/end are 1-based lines for repo.read. " +
			"Narrow with `path` (a directory or file prefix) and `kinds`; page with `after`. A file with many names shows the outer ones " +
			"first (symbols_left says how many are left out). Files that may hold credentials are left out.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo"],"properties":{` + repoProp + `,` +
			`"path":{"type":"string","maxLength":512,"description":"a directory or file prefix"},` +
			`"kinds":{"type":"array","maxItems":6,"uniqueItems":true,"items":{"enum":` + string(kinds) + `}},` +
			`"limit":{"type":"integer","minimum":1,"maximum":200,"description":"files per page (default 50)"},` +
			`"max_symbols":{"type":"integer","minimum":1,"maximum":200,"description":"names per file (default 40)"},` +
			`"after":{"type":"string","maxLength":1024,"description":"the previous call's next_after"},` + branchProp + `}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Repo       string   `json:"repo"`
				Path       string   `json:"path"`
				Kinds      []string `json:"kinds"`
				Limit      int      `json:"limit"`
				MaxSymbols int      `json:"max_symbols"`
				After      string   `json:"after"`
				Branch     string   `json:"branch"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			want := map[string]bool{}
			for _, k := range in.Kinds {
				if !slices.Contains(outlineKinds, k) {
					return nil, inputErr("kinds: %q is not one of %s", k, strings.Join(outlineKinds, ", "))
				}
				want[k] = true
			}
			limit := in.Limit
			if limit <= 0 {
				limit = outlineFiles
			}
			limit = min(limit, 200)
			maxSyms := in.MaxSymbols
			if maxSyms <= 0 {
				maxSyms = outlineSymbols
			}
			maxSyms = min(maxSyms, outlineMaxSymbols)
			dir, err := w.clone(in.Repo)
			if err != nil {
				return nil, err
			}
			prefix := ""
			if in.Path != "" {
				if prefix, err = cleanRel(in.Path); err != nil {
					return nil, err
				}
			}
			// a large task's branch: its blobs, read from git's objects (the worktree stays as it is)
			var blobs map[string]treeBlob
			var names []string
			if in.Branch != "" {
				ref, err := w.atBranch(ctx, dir, in.Branch)
				if err != nil {
					return nil, err
				}
				all, err := w.treeBlobs(ctx, dir, ref, prefix)
				if err != nil {
					return nil, err
				}
				blobs = map[string]treeBlob{}
				for _, b := range all {
					if readableBlob(b) {
						blobs[b.path] = b
						names = append(names, b.path)
					}
				}
			} else {
				args := []string{"ls-files", "-z", "--cached", "--others", "--exclude-standard"}
				if prefix != "" {
					args = append(args, "--", prefix)
				}
				out, err := w.git(ctx, dir, nil, "", args...)
				if err != nil {
					return nil, err
				}
				names = strings.Split(out, "\x00")
			}
			seen := map[string]bool{}
			var files []string
			for _, f := range names {
				k := outlineKind(f)
				if f == "" || seen[f] || k == "" || (len(want) > 0 && !want[k]) || secretName(f) || f <= in.After {
					continue
				}
				seen[f] = true // a file in conflict is listed once per stage
				files = append(files, f)
			}
			sort.Strings(files)
			var links map[string]bool
			if blobs == nil {
				if links, err = w.trackedLinks(ctx, dir); err != nil {
					return nil, err
				}
			}
			res := map[string]any{"repo": in.Repo, "total_after": len(files)}
			var page []outlineFile
			size, masked := 0, 0
			for _, f := range files {
				// a call that timed out stops here instead of reading on (each file is bounded)
				if err := ctx.Err(); err != nil {
					return nil, err
				}
				if len(page) == limit {
					res["next_after"] = page[len(page)-1].Path
					break
				}
				var entry outlineFile
				var n int
				var ok bool
				if b, at := blobs[f]; at {
					entry, n, ok = outlineEntry(f, b.size, func() ([]byte, error) { return w.blobBytes(ctx, dir, b) }, maxSyms)
				} else {
					entry, n, ok = outlineOne(dir, f, links, maxSyms)
				}
				if !ok {
					continue
				}
				b, _ := json.Marshal(entry)
				// one file alone stays inside a page too: escaped names (<, &, non-Latin text) can
				// make 200 of them larger than the whole answer may be
				for len(b) > outlineBytes && len(entry.Symbols) > 0 {
					kept, left := keepSymbols(entry.Symbols, len(entry.Symbols)/2) // the outer ones stay
					entry.Symbols, entry.Left = kept, entry.Left+left
					b, _ = json.Marshal(entry)
				}
				if size+len(b) > outlineBytes && len(page) > 0 {
					res["next_after"] = page[len(page)-1].Path
					break
				}
				size += len(b)
				masked += n
				page = append(page, entry)
			}
			if page == nil {
				page = []outlineFile{}
			}
			res["files"] = page
			res["masked"] = masked
			return res, nil
		},
	}
}

// outlineOne reads one listed file the way repo.read would (inside the clone, no link on the way)
// and returns its entry, how many names were masked, and false for a file the workshop does not read.
func outlineOne(dir, f string, links map[string]bool, maxSyms int) (outlineFile, int, bool) {
	for i := range f {
		if f[i] == '/' && links[f[:i]] {
			return outlineFile{}, 0, false
		}
	}
	if links[f] {
		return outlineFile{}, 0, false
	}
	rel, abs, err := confine(dir, f, false)
	if err != nil {
		return outlineFile{}, 0, false
	}
	st, err := os.Lstat(abs)
	if err != nil || !st.Mode().IsRegular() {
		return outlineFile{}, 0, false
	}
	return outlineEntry(rel, st.Size(), func() ([]byte, error) { return os.ReadFile(abs) }, maxSyms)
}

// outlineEntry builds the entry of one file from its size and its content (read only when the
// file is small enough): the worktree's file or a blob of a large task's branch.
func outlineEntry(rel string, size int64, read func() ([]byte, error), maxSyms int) (outlineFile, int, bool) {
	entry := outlineFile{Path: rel, Kind: outlineKind(rel), Bytes: size, Symbols: []outlineSymbol{}}
	if size > maxFileBytes {
		entry.Skipped = "too large"
		return entry, 0, true
	}
	data, err := read()
	if err != nil {
		return outlineFile{}, 0, false
	}
	text, err := asText(rel, data)
	if err != nil {
		entry.Skipped = "not text"
		return entry, 0, true
	}
	entry.Lines = len(splitLines(text))
	syms, left := keepSymbols(symbolsOf(rel, text), maxSyms)
	masked := 0
	for i := range syms {
		name, n := tools.Mask(syms[i].Name)
		syms[i].Name = shortName(name)
		masked += n
	}
	entry.Symbols, entry.Left = syms, left
	return entry, masked, true
}
