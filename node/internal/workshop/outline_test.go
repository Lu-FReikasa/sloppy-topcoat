package workshop

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"
)

// names returns "name kind start-end" per symbol, the way the tests compare them.
func names(syms []outlineSymbol) []string {
	out := make([]string, 0, len(syms))
	for _, s := range syms {
		out = append(out, fmt.Sprintf("%s %s %d-%d", s.Name, s.Kind, s.Start, s.End))
	}
	return out
}

func sameList(t *testing.T, got, want []string) {
	t.Helper()
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("got\n  %s\nwant\n  %s", strings.Join(got, "\n  "), strings.Join(want, "\n  "))
	}
}

func TestOutlinePython(t *testing.T) {
	src := strings.Join([]string{
		`"""Module doc.`,
		`def not_a_function():`, // inside the docstring
		`"""`,
		`import os`,
		``,
		`MAX = 3`,
		`TABLE = {`,
		`    "a": 1,`,
		`}`,
		``,
		``,
		`@dataclass`,
		`class Store:`,
		`    limit = 2`,
		`    def load(self, x):`,
		`        """Text`,
		`class InDoc:`,
		`        """`,
		`        return x`,
		`# a comment at column 0 inside the class`,
		`    async def save(`,
		`        self,`,
		`    ) -> None:`,
		`        def inner():`,
		`            pass`,
		`        return inner`,
		``,
		``,
		`def top(a, b):`,
		`    return a == b`,
	}, "\r\n") // a CRLF checkout
	sameList(t, names(symbolsOf("hub/store.py", src)), []string{
		"MAX const 6-6",
		"TABLE const 7-9",
		"Store class 12-26",
		"Store.load method 15-19",
		"Store.save method 21-26",
		"Store.save.inner function 24-25",
		"top function 29-30",
	})
}

func TestOutlineTypeScript(t *testing.T) {
	src := strings.Join([]string{
		`import { x } from "./x";`,
		``,
		`/** The props. */`,
		`export interface Props {`,
		`  name: string;`,
		`}`,
		`export const LIMIT = 3;`,
		`const label = (n: number) =>`,
		"  `n=${n} {`;",
		`export default function Panel({ name }: Props) {`,
		`  const onClick = () => {`,
		`    if (name) {`,
		`      run(name, () => {`,
		`      });`,
		`    }`,
		`  };`,
		`  return <p>Don't {name}</p>;`,
		`}`,
		`export class Store {`,
		`  private items: string[] = [];`,
		`  async load(id: string): Promise<void> {`,
		`    this.items.push(id);`,
		`  }`,
		`  get size() {`,
		`    return this.items.length;`,
		`  }`,
		`}`,
	}, "\n")
	sameList(t, names(symbolsOf("web/src/Panel.tsx", src)), []string{
		"Props interface 3-6",
		"LIMIT const 7-7",
		"label function 8-9",
		"Panel component 10-18",
		"Panel.onClick function 11-16",
		"Store class 19-27",
		"Store.load method 21-23",
		"Store.size method 24-26",
	})
}

func TestOutlineGo(t *testing.T) {
	src := strings.Join([]string{
		`package demo`,
		``,
		`// Store keeps things.`,
		`type Store struct {`,
		`	items []string`,
		`}`,
		``,
		`type Loader interface {`,
		`	Load() error`,
		`}`,
		``,
		`type ID string`,
		``,
		`// Load reads one.`,
		`func (s *Store) Load(id string) error {`,
		"	q := `{ raw`",
		"	_ = '{'",
		`	return nil`,
		`}`,
		``,
		`func New[T any](x T) *Store {`,
		`	return &Store{}`,
		`}`,
	}, "\n")
	sameList(t, names(symbolsOf("node/demo.go", src)), []string{
		"Store type 3-6",
		"Loader interface 8-10",
		"ID type 12-12",
		"Store.Load method 14-19",
		"New function 21-23",
	})
}

func TestOutlineJSONMarkdownCSS(t *testing.T) {
	jsonText := "{\n  \"app\": {\n    \"loading\": \"Loading\"\n  },\n  \"login\": {\n    \"heading\": \"Sign in\",\n    \"submit\": \"Go\"\n  }\n}\n"
	sameList(t, names(symbolsOf("web/src/i18n/en.json", jsonText)), []string{
		"app key 2-4", "app.loading key 3-3", "login key 5-8", "login.heading key 6-6", "login.submit key 7-7",
	})
	md := "# Title\n\nText\n\n## Setup\n\n```sh\n# not a heading\n```\n\n### Detail\n\n## Use\nmore\n"
	sameList(t, names(symbolsOf("docs/GUIDE.md", md)), []string{
		"# Title heading 1-14", "## Setup heading 5-12", "### Detail heading 11-12", "## Use heading 13-14",
	})
	css := ".app,\n.panel {\n  color: red;\n}\n@media (max-width: 400px) {\n  .app { color: blue; }\n}\n.chip  .label { x: 1 }\n"
	sameList(t, names(symbolsOf("web/src/styles.css", css)), []string{
		".panel rule 2-4", "@media (max-width: 400px) rule 5-7", ".chip .label rule 8-8",
	})
	if got := symbolsOf("notes.txt", "# x\n"); len(got) != 0 {
		t.Fatalf("an unknown kind has no names: %v", got)
	}
}

func TestOutlineKeepsOuterNamesFirst(t *testing.T) {
	var b strings.Builder
	for i := range 3 {
		fmt.Fprintf(&b, "class C%d:\n", i)
		for j := range 4 {
			fmt.Fprintf(&b, "    def m%d(self):\n        pass\n", j)
		}
	}
	kept, left := keepSymbols(symbolsOf("a.py", b.String()), 5)
	sameList(t, func() []string {
		var n []string
		for _, s := range kept {
			n = append(n, s.Name)
		}
		return n
	}(), []string{"C0", "C0.m0", "C0.m1", "C1", "C2"})
	if left != 10 {
		t.Fatalf("left = %d, want 10", left)
	}
}

// A hostile file (declarations that never close, deep nesting) must cost about as much as a normal
// one: before the scan budget every declaration counted braces to the end of the file, minutes of
// CPU for one 1 MB file, and nested names grew to gigabytes.
func TestOutlineHostileFilesStayCheap(t *testing.T) {
	const mb = maxFileBytes
	for name, line := range map[string]string{
		"open.ts":   "const a = (\n",
		"nest.ts":   " class A {\n",
		"open.go":   "func a(\n",
		"open.css":  "a{\n",
		"nest.py":   "", // built below: each def one level deeper
		"nest.json": "",
	} {
		text := strings.Repeat(line, mb/max(len(line), 1))
		switch name {
		case "nest.py":
			var b strings.Builder
			for i := 0; b.Len() < mb; i++ {
				fmt.Fprintf(&b, "%sdef %s():\n", strings.Repeat(" ", i), strings.Repeat("n", 200))
			}
			text = b.String()
		case "nest.json":
			var b strings.Builder
			for i := 0; b.Len() < mb; i++ {
				fmt.Fprintf(&b, "%s\"%s\": {\n", strings.Repeat(" ", i), strings.Repeat("k", 200))
			}
			text = b.String()
		}
		start := time.Now()
		syms := symbolsOf(name, text)
		if took := time.Since(start); took > 15*time.Second {
			t.Errorf("%s: %d names took %v", name, len(syms), took)
		}
		for _, s := range syms {
			if len(s.Name) > outlineNameBytes+len("…") || s.Start < 1 || s.End < s.Start {
				t.Fatalf("%s: %d bytes of name, lines %d-%d", name, len(s.Name), s.Start, s.End)
			}
		}
	}
}

func TestRepoOutline(t *testing.T) {
	f := newFixture(t, nil)
	f.must(t, "repo.sync", repoIn{"repo": "demo"})
	dir, _ := f.w.clone("demo")
	// a new (untracked) file is mapped too, like repo.list shows it
	write(t, dir, "web/src/App.tsx", "export function App() {\n  return null;\n}\n")
	write(t, dir, "web/src/big.json", "{\n"+strings.Repeat("  \"k\": 1,\n", 10)+"  \"z\": 2\n}\n")

	all := f.must(t, "repo.outline", repoIn{"repo": "demo"})
	var got struct {
		Files      []outlineFile `json:"files"`
		TotalAfter int           `json:"total_after"`
		NextAfter  string        `json:"next_after"`
	}
	remarshal(t, all, &got)
	var paths []string
	for _, file := range got.Files {
		paths = append(paths, file.Path)
	}
	// no .env, no server.key, no link, no workflow yaml (not a known kind), sorted
	sameList(t, paths, []string{"README.md", "app/main.py", "app/util.py", "config.py", "web/src/App.tsx", "web/src/big.json"})
	main := got.Files[1]
	if main.Kind != "py" || main.Lines != 6 || len(main.Symbols) != 2 || main.Symbols[1].Name != "twice" || main.Symbols[1].Start != 5 {
		t.Fatalf("app/main.py: %+v", main)
	}
	if cfg := got.Files[3]; strings.Contains(fmt.Sprint(cfg), "hunter2") {
		t.Fatalf("the outline shows names, never values: %+v", cfg)
	}

	// narrowed by path and kind, paged
	py := f.must(t, "repo.outline", repoIn{"repo": "demo", "path": "app", "kinds": []string{"py"}, "limit": 1})
	remarshal(t, py, &got)
	if len(got.Files) != 1 || got.Files[0].Path != "app/main.py" || got.TotalAfter != 2 || got.NextAfter != "app/main.py" {
		t.Fatalf("first page: %+v", got)
	}
	got.NextAfter = ""
	remarshal(t, f.must(t, "repo.outline", repoIn{"repo": "demo", "path": "app", "after": "app/main.py"}), &got)
	if len(got.Files) != 1 || got.Files[0].Path != "app/util.py" || got.NextAfter != "" {
		t.Fatalf("second page: %+v", got)
	}

	// a file with more names than asked for says how many are left out
	remarshal(t, f.must(t, "repo.outline", repoIn{"repo": "demo", "path": "web/src/big.json", "max_symbols": 3}), &got)
	if len(got.Files) != 1 || len(got.Files[0].Symbols) != 3 || got.Files[0].Left != 8 {
		t.Fatalf("cut file: %+v", got.Files)
	}

	// one file whose names escape to more than a page (`<` is six bytes in JSON) is cut to fit it,
	// the outer names kept
	var css strings.Builder
	for i := range 200 {
		fmt.Fprintf(&css, ".r%d %s { }\n", i, strings.Repeat("<", 110))
	}
	write(t, dir, "web/src/wide.css", css.String())
	raw := f.must(t, "repo.outline", repoIn{"repo": "demo", "path": "web/src/wide.css", "max_symbols": 200})
	if b, _ := json.Marshal(raw); len(b) > outlineBytes+1024 {
		t.Fatalf("one file's answer is %d bytes", len(b))
	}
	remarshal(t, raw, &got)
	if wide := got.Files[0]; len(wide.Symbols) == 0 || wide.Left == 0 || len(wide.Symbols)+wide.Left != 200 {
		t.Fatalf("wide.css: %d names, %d left", len(wide.Symbols), wide.Left)
	}

	_, err := f.call(t, "repo.outline", repoIn{"repo": "demo", "path": "../up"})
	refused(t, err, "inside the repository")
	_, err = f.call(t, "repo.outline", repoIn{"repo": "demo", "kinds": []string{"rs"}})
	refused(t, err, "kinds")
}

func remarshal(t *testing.T, from any, to any) {
	t.Helper()
	b, err := json.Marshal(from)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, to); err != nil {
		t.Fatal(err)
	}
}
