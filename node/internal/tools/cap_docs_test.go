package tools

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// docsTree is a knowledge folder with ordinary notes and everything the exclude list must hide.
func docsTree(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	write := func(name, content string) {
		p := filepath.Join(dir, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(p), 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("README.md", "# Home lab\n\nThe web stack runs on vm-docker1.\n")
	write("servers/web.md", "# Web\n\n## Restart\n\nUse `docker compose restart web`.\n")
	write("servers/notes.txt", "plain notes")
	write("servers/image.png", "not text")
	write("SENSIBEL/passwords.md", "# do not read\nadmin: hunter2hunter2")
	write("servers/Zugangsdaten-SENSIBEL.md", "# keys")
	write("servers/nas-password.md", "# nope")
	write("ZUGANGSDATEN.md", "# nope")
	write("keys/host.key", "-----BEGIN PRIVATE KEY-----")
	write("servers/server.env.md", "# env notes") // matches "*.env"? no: ".md" — see the test
	write(".git/config.md", "# git")
	write("custom-private/plan.md", "# plan")
	return dir
}

func TestDocsListShowsOnlyTextFilesOutsideTheExcludeList(t *testing.T) {
	dir := docsTree(t)
	out, err := invoke(t, Options{DocsRoots: []string{dir}}, "docs.list", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	var paths []string
	for _, f := range out["files"].([]any) {
		paths = append(paths, f.(map[string]any)["path"].(string))
	}
	want := "README.md custom-private/plan.md servers/notes.txt servers/server.env.md servers/web.md"
	if strings.Join(paths, " ") != want {
		t.Fatalf("paths = %v", paths)
	}
	if out["excluded"].(float64) < 5 || out["truncated"] != false {
		t.Fatalf("out = %v", out)
	}
	// nothing excluded is named anywhere in the answer
	text := fmt.Sprint(out)
	for _, bad := range []string{"SENSIBEL", "ZUGANGSDATEN", "password", "host.key", ".git"} {
		if strings.Contains(strings.ToLower(text), strings.ToLower(bad)) {
			t.Fatalf("the listing names %q: %s", bad, text)
		}
	}
}

func TestDocsListExtraExcludeAddsToTheFixedList(t *testing.T) {
	dir := docsTree(t)
	out, err := invoke(t, Options{DocsRoots: []string{dir}, DocsExclude: []string{"custom-*"}}, "docs.list", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(fmt.Sprint(out), "custom-private") || !strings.Contains(fmt.Sprint(out), "README.md") {
		t.Fatalf("out = %v", out)
	}
	// the fixed list still holds (an extra list never shortens it)
	if strings.Contains(fmt.Sprint(out), "SENSIBEL") {
		t.Fatal("fixed exclude list was dropped")
	}
}

func TestDocsListPages(t *testing.T) {
	dir := docsTree(t)
	o := Options{DocsRoots: []string{dir}}
	first, err := invoke(t, o, "docs.list", `{"limit":2}`)
	if err != nil {
		t.Fatal(err)
	}
	if first["truncated"] != true || first["next_after"] != "custom-private/plan.md" && first["next_after"] != "servers/notes.txt" {
		t.Fatalf("first = %v", first)
	}
	second, err := invoke(t, o, "docs.list", fmt.Sprintf(`{"limit":10,"after":%q}`, first["next_after"]))
	if err != nil {
		t.Fatal(err)
	}
	if len(first["files"].([]any))+len(second["files"].([]any)) != 5 || second["truncated"] != false {
		t.Fatalf("first=%v second=%v", first, second)
	}
}

func TestDocsDefaultIsDenyAndRootsAreChecked(t *testing.T) {
	dir := docsTree(t)
	if _, ok := NewRegistry(Options{}).Get("docs.list"); ok {
		t.Fatal("a node without released directories must not offer docs.list")
	}
	o := Options{DocsRoots: []string{dir, t.TempDir()}}
	if _, err := invoke(t, o, "docs.list", `{}`); err == nil || !strings.Contains(err.Error(), "root is required") {
		t.Fatalf("err = %v", err)
	}
	if _, err := invoke(t, o, "docs.list", `{"root":"/etc"}`); err == nil || !strings.Contains(err.Error(), "not released") {
		t.Fatalf("err = %v", err)
	}
	if _, err := invoke(t, o, "docs.list", fmt.Sprintf(`{"root":%q}`, dir)); err != nil {
		t.Fatal(err)
	}
}

func TestDocsReadRefusesEverythingThatIsExcludedOrOutside(t *testing.T) {
	dir := docsTree(t)
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "secret-plan.md"), []byte("# outside"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(outside, "plain.md"), []byte("# outside but plain"), 0o600); err != nil {
		t.Fatal(err)
	}
	// links: one to a plain file outside, one to an excluded file inside
	must(t, os.Symlink(filepath.Join(outside, "plain.md"), filepath.Join(dir, "link-out.md")))
	must(t, os.Symlink(filepath.Join(dir, "SENSIBEL", "passwords.md"), filepath.Join(dir, "link-in.md")))
	o := Options{DocsRoots: []string{dir}}
	for _, p := range []string{
		"SENSIBEL/passwords.md", "servers/Zugangsdaten-SENSIBEL.md", "servers/nas-password.md", "ZUGANGSDATEN.md",
		"keys/host.key", ".git/config.md", "../x.md", "/etc/passwd", "servers/../../x.md", "servers/image.png",
		"link-out.md", "link-in.md", "missing.md", "", "servers//web.md", `servers\web.md`,
	} {
		_, err := invoke(t, o, "docs.read", fmt.Sprintf(`{"path":%q}`, p))
		if err == nil {
			t.Errorf("%q was read", p)
			continue
		}
		// the same words for every refusal: nothing tells whether the file exists or why it is refused
		if p != "" && !strings.Contains(err.Error(), "not available") {
			t.Errorf("%q: err = %v", p, err)
		}
	}
	// the list does not show the links either
	out, _ := invoke(t, o, "docs.list", `{}`)
	if strings.Contains(fmt.Sprint(out), "link-") {
		t.Fatalf("links are listed: %v", out)
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Skipf("symlinks are not available here: %v", err)
	}
}

func TestDocsReadReturnsPagesAndMasksSecrets(t *testing.T) {
	dir := t.TempDir()
	var b strings.Builder
	b.WriteString("# Big note\n\nPassword=hunter2hunter2 stays out.\n\n")
	for i := 0; b.Len() < 60<<10; i++ {
		fmt.Fprintf(&b, "Line %d: ünïcödé text to make the file long enough for paging.\n", i)
	}
	must(t, os.WriteFile(filepath.Join(dir, "big.md"), []byte(b.String()), 0o600))
	o := Options{DocsRoots: []string{dir}}
	var got strings.Builder
	offset := 0.0
	for pages := 0; ; pages++ {
		if pages > 10 {
			t.Fatal("too many pages")
		}
		out, err := invoke(t, o, "docs.read", fmt.Sprintf(`{"path":"big.md","offset":%d}`, int(offset)))
		if err != nil {
			t.Fatal(err)
		}
		got.WriteString(out["content"].(string))
		if len(out["content"].(string)) > DocsReadBytes {
			t.Fatalf("a page of %d bytes", len(out["content"].(string)))
		}
		next, ok := out["next_offset"].(float64)
		if !ok {
			break
		}
		offset = next
	}
	text := got.String()
	if strings.Contains(text, "hunter2hunter2") || !strings.Contains(text, "masked") {
		t.Fatalf("the secret was not masked: %.200s", text)
	}
	if strings.Count(text, "Line ") < 100 || strings.ContainsRune(text, '�') {
		t.Fatalf("paging lost or split text (%d lines)", strings.Count(text, "Line "))
	}
	// the pages together are the file (apart from the masked value)
	if !strings.HasSuffix(text, b.String()[len(b.String())-40:]) {
		t.Fatal("the last page is missing")
	}
}

func TestDocsExcludedMatchesEverySegmentCaseInsensitively(t *testing.T) {
	for p, want := range map[string]bool{
		"a/b/c.md":                    false,
		"SENSIBEL/x.md":               true,
		"a/sensibel-notes/x.md":       true,
		"a/x-ZUGANGSDATEN.md":         true,
		"x.PEM":                       true,
		"deploy/.env.production":      true,
		"a/My-Password-List.md":       true,
		"a/PASSWORT.txt":              true,
		"notes/secrets-of-cooking.md": true,
		"a/.git/HEAD":                 true,
		"keyboard.md":                 false,
		"servers/web.md":              false,
	} {
		if got := DocsExcluded(p, nil); got != want {
			t.Errorf("DocsExcluded(%q) = %v, want %v", p, got, want)
		}
	}
	if !DocsExcluded("x/private/y.md", []string{"private"}) || DocsExcluded("x/y.md", []string{"private"}) {
		t.Error("extra globs do not work")
	}
	if ValidDocsGlob("[") || ValidDocsGlob("") || !ValidDocsGlob("*.tmp") {
		t.Error("ValidDocsGlob is wrong")
	}
}

func TestDocsCapabilitiesAreOnlyOfferedWithARoot(t *testing.T) {
	has := func(o Options, name string) bool {
		for _, n := range NewRegistry(o).Names() {
			if n == name {
				return true
			}
		}
		return false
	}
	if has(Options{}, "docs.list") || has(Options{}, "docs.read") {
		t.Fatal("offered without a released directory")
	}
	if !has(Options{DocsRoots: []string{t.TempDir()}}, "docs.read") {
		t.Fatal("not offered with a released directory")
	}
}
