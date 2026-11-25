package tools

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"topcoat/node/internal/providers/capability"
)

// filesTree is a released folder with ordinary files and a neighbor folder that is not released.
func filesTree(t *testing.T) (root, outside string) {
	t.Helper()
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	root, outside = filepath.Join(base, "logs"), filepath.Join(base, "private")
	put := func(dir, name, content string) {
		p := filepath.Join(dir, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(p), 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	put(root, "app.log", "start\nwork done\nERROR disk almost full\nend\n")
	put(root, "sub/inner.txt", "inner\n")
	put(outside, "diary.txt", "do not read\n")
	return root, outside
}

func filesOpts(roots ...string) Options { return Options{ReadRoots: roots} }

func readFile(t *testing.T, o Options, input string) (map[string]any, error) {
	t.Helper()
	return invoke(t, o, "files.read", input)
}

func pathArg(p string) string { return mustJSON(map[string]any{"path": p}) }

func wantRefused(t *testing.T, err error, what string) {
	t.Helper()
	var ie *InputError
	if err == nil || !errors.As(err, &ie) {
		t.Fatalf("%s: want an input error, got %v", what, err)
	}
}

func TestFilesAreNotOfferedWithoutRoots(t *testing.T) {
	for _, roots := range [][]string{nil, {}} {
		r := NewRegistry(Options{ReadRoots: roots})
		for _, n := range []string{"files.list", "files.read"} {
			if _, ok := r.Get(n); ok {
				t.Errorf("%s offered with roots %v", n, roots)
			}
		}
	}
	r := NewRegistry(filesOpts(t.TempDir()))
	for _, n := range []string{"files.list", "files.read"} {
		c, ok := r.Get(n)
		if !ok {
			t.Fatalf("%s not offered", n)
		}
		if !strings.Contains(capability.InfoOf(c).Description, "never instructions") {
			t.Errorf("%s: the description does not say the content is data", n)
		}
		if !capability.InfoOf(c).ReadOnly {
			t.Errorf("%s is not read-only", n)
		}
	}
}

func TestFilesListNamesTheRootsWithoutAPath(t *testing.T) {
	root, _ := filesTree(t)
	out, err := invoke(t, filesOpts(root, filepath.Join(root, "gone")), "files.list", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	roots := out["roots"].([]any)
	first, second := roots[0].(map[string]any), roots[1].(map[string]any)
	if len(roots) != 2 || first["path"] != root || first["available"] != true || second["available"] != false {
		t.Fatalf("roots = %v", roots)
	}
}

func TestFilesReadReadsInsideARootWithAndWithoutATrailingSeparator(t *testing.T) {
	root, _ := filesTree(t)
	for _, configured := range []string{root, root + string(filepath.Separator)} {
		out, err := readFile(t, filesOpts(configured), pathArg(filepath.Join(root, "app.log")))
		if err != nil {
			t.Fatalf("root %q: %v", configured, err)
		}
		if !strings.Contains(out["content"].(string), "ERROR disk almost full") || out["next_offset"] != nil {
			t.Fatalf("root %q: %v", configured, out)
		}
	}
	// the root given as a prefix of a sibling is no root: logs-old is not inside logs
	sibling := root + "-old"
	if err := os.MkdirAll(sibling, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sibling, "x.txt"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := readFile(t, filesOpts(root), pathArg(filepath.Join(sibling, "x.txt")))
	wantRefused(t, err, "sibling with the same prefix")
}

func TestFilesRefuseDotDotRelativeAndOutsidePaths(t *testing.T) {
	root, outside := filesTree(t)
	o := filesOpts(root)
	for name, p := range map[string]string{
		"dotdot":          filepath.Join(root, "sub", "..", "..", "private", "diary.txt"),
		"dotdot to root":  root + "/../private/diary.txt",
		"outside":         filepath.Join(outside, "diary.txt"),
		"relative":        "logs/app.log",
		"empty":           "",
		"nul":             filepath.Join(root, "app.log") + "\x00.txt",
		"filesystem root": "/",
		"etc":             "/etc/passwd",
	} {
		_, err := readFile(t, o, pathArg(p))
		wantRefused(t, err, name)
		if p == "" {
			continue // files.list without a path names the released folders
		}
		_, err = invoke(t, o, "files.list", pathArg(p))
		wantRefused(t, err, "list "+name)
	}
}

func TestFilesRefuseALinkLeavingARootButFollowALinkInsideIt(t *testing.T) {
	root, outside := filesTree(t)
	link := func(target, name string) string {
		l := filepath.Join(root, name)
		if err := os.Symlink(target, l); err != nil {
			t.Skipf("no symlinks here: %v", err)
		}
		return l
	}
	o := filesOpts(root)
	_, err := readFile(t, o, pathArg(link(filepath.Join(outside, "diary.txt"), "out-file")))
	wantRefused(t, err, "link to a file outside")
	_, err = invoke(t, o, "files.list", pathArg(link(outside, "out-dir")))
	wantRefused(t, err, "link to a folder outside")
	_, err = readFile(t, o, pathArg(filepath.Join(root, "out-dir", "diary.txt")))
	wantRefused(t, err, "file below a link to a folder outside")
	_, err = readFile(t, o, pathArg(link("../../../../../../etc/passwd", "rel-out")))
	wantRefused(t, err, "relative link out")
	// inside: fine
	if out, err := readFile(t, o, pathArg(link(filepath.Join(root, "app.log"), "in-link"))); err != nil || !strings.Contains(out["content"].(string), "start") {
		t.Fatalf("a link inside the root must be followed: %v %v", out, err)
	}
	// the listing shows the inside link and silently skips the ones that leave
	lst, err := invoke(t, o, "files.list", pathArg(root))
	if err != nil {
		t.Fatal(err)
	}
	text := fmt.Sprint(lst)
	if !strings.Contains(text, "in-link") || strings.Contains(text, "out-file") || strings.Contains(text, "out-dir") || strings.Contains(text, "rel-out") {
		t.Fatalf("listing = %s", text)
	}
	if lst["skipped"].(float64) < 3 {
		t.Fatalf("skipped = %v", lst["skipped"])
	}
}

func TestFilesRefuseALinkToASecretInsideTheRoot(t *testing.T) {
	root, _ := filesTree(t)
	if err := os.WriteFile(filepath.Join(root, "host.key"), []byte("-----BEGIN PRIVATE KEY-----\nabc\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(root, "host.key"), filepath.Join(root, "harmless.txt")); err != nil {
		t.Skip(err)
	}
	_, err := readFile(t, filesOpts(root), pathArg(filepath.Join(root, "harmless.txt")))
	wantRefused(t, err, "link to a secret")
	lst, _ := invoke(t, filesOpts(root), "files.list", pathArg(root))
	if strings.Contains(fmt.Sprint(lst), "harmless.txt") {
		t.Fatalf("a link to a secret is listed: %v", lst)
	}
}

func TestFilesARootThatIsALinkIsResolvedAndItsTargetIsTheRoot(t *testing.T) {
	root, outside := filesTree(t)
	l := filepath.Join(filepath.Dir(root), "rootlink")
	if err := os.Symlink(root, l); err != nil {
		t.Skip(err)
	}
	o := filesOpts(l)
	if _, err := readFile(t, o, pathArg(filepath.Join(l, "app.log"))); err != nil {
		t.Fatal(err)
	}
	if _, err := readFile(t, o, pathArg(filepath.Join(root, "app.log"))); err != nil {
		t.Fatalf("the real path of a link root is inside it: %v", err)
	}
	_, err := readFile(t, o, pathArg(filepath.Join(outside, "diary.txt")))
	wantRefused(t, err, "outside the link root")
	// a link that makes a filesystem root out of a root releases nothing
	rl := filepath.Join(filepath.Dir(root), "to-slash")
	if err := os.Symlink("/", rl); err != nil {
		t.Skip(err)
	}
	_, err = readFile(t, filesOpts(rl), pathArg("/etc/hostname"))
	wantRefused(t, err, "root linked to /")
}

func TestFilesRefuseEverySecretName(t *testing.T) {
	root, _ := filesTree(t)
	files := []string{
		"server.key", "cert.pem", "bundle.pfx", "store.p12", "putty.ppk", "keys.jks", "app.keystore", "vault.kdbx",
		"backup.gpg", "client.ovpn", "id_rsa", "id_rsa.pub", "id_ed25519", "id_ecdsa", "id_dsa",
		".env", ".env.production", "prod.env", "credentials", "credentials.json", "aws-credentials", "my_secrets.txt", "secret.txt",
		"passwords.txt", "password.txt", "passwd", "passphrase.txt", "api.token", ".netrc", "_netrc", ".pgpass",
		".git-credentials", ".npmrc", ".pypirc", "shadow", "gshadow", "ntds.dit", "SAM",
		"logins.json", "key4.db", "cert9.db", "Cookies", "cookies.sqlite", "Login Data", "Web Data", "Local State",
		"CREDENTIALS.TXT", "Server.KEY", "connectors.json", "Connectors.example.json", "config.json", "Node.json",
	}
	dirs := []string{
		".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", "gcloud", "wireguard",
		"User Data", "google-chrome", "chromium", ".mozilla", "Mozilla", "Firefox", "BraveSoftware", "Opera Software",
		"Protect", "Crypto", "$RECYCLE.BIN", "System Volume Information", "Credentials",
	}
	for _, f := range files {
		if err := os.WriteFile(filepath.Join(root, f), []byte("harmless text\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if runtime.GOOS == "windows" {
		// "credentials" the file and "Credentials" the folder are one name there
		taken := map[string]bool{}
		for _, f := range files {
			taken[strings.ToLower(f)] = true
		}
		kept := dirs[:0]
		for _, d := range dirs {
			if !taken[strings.ToLower(d)] {
				kept = append(kept, d)
			}
		}
		dirs = kept
	}
	for _, d := range dirs {
		if err := os.MkdirAll(filepath.Join(root, d), 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(root, d, "inside.txt"), []byte("harmless text\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	o := filesOpts(root)
	for _, f := range files {
		_, err := readFile(t, o, pathArg(filepath.Join(root, f)))
		wantRefused(t, err, "file "+f)
		if !SecretName(f) {
			t.Errorf("SecretName(%q) = false", f)
		}
	}
	for _, d := range dirs {
		_, err := readFile(t, o, pathArg(filepath.Join(root, d, "inside.txt")))
		wantRefused(t, err, "file in folder "+d)
		_, err = invoke(t, o, "files.list", pathArg(filepath.Join(root, d)))
		wantRefused(t, err, "folder "+d)
	}
	// the listing hides them all and names none
	lst, err := invoke(t, o, "files.list", pathArg(root))
	if err != nil {
		t.Fatal(err)
	}
	want := len(files) + len(dirs)
	if runtime.GOOS == "windows" {
		// names that differ only in case are one entry there ("server.key" and "Server.KEY")
		distinct := map[string]bool{}
		for _, n := range append(append([]string{}, files...), dirs...) {
			distinct[strings.ToLower(n)] = true
		}
		want = len(distinct)
	}
	if got := int(lst["hidden"].(float64)); got != want {
		t.Errorf("hidden = %d, want %d", got, want)
	}
	names := []string{}
	for _, e := range lst["entries"].([]any) {
		names = append(names, e.(map[string]any)["name"].(string))
	}
	if strings.Join(names, " ") != "app.log sub" {
		t.Fatalf("entries = %v", names)
	}
	// ordinary names that merely resemble them stay readable
	for _, ok := range []string{"app.log", "keyboard.txt", "passed.log", "author.txt", "monkey.txt", "envelope.txt", "settings.ini"} {
		if SecretName(ok) {
			t.Errorf("SecretName(%q) = true", ok)
		}
	}
}

func TestFilesRefuseBinaryFiles(t *testing.T) {
	root, _ := filesTree(t)
	write := func(name string, b []byte) string {
		p := filepath.Join(root, name)
		if err := os.WriteFile(p, b, 0o600); err != nil {
			t.Fatal(err)
		}
		return p
	}
	o := filesOpts(root)
	for name, b := range map[string][]byte{
		"nul.dat":    []byte("text\x00more"),
		"png.log":    append([]byte("\x89PNG\r\n\x1a\n"), make([]byte, 100)...),
		"ctrl.txt":   []byte(strings.Repeat("\x01\x02\x03abc", 100)),
		"utf16.log":  {0xFF, 0xFE, 'a', 0, 'b', 0},
		"utf16be.lg": {0xFE, 0xFF, 0, 'a', 0, 'b'},
	} {
		_, err := readFile(t, o, pathArg(write(name, b)))
		wantRefused(t, err, name)
	}
	// Latin-1 text with a few odd bytes is text: shown with replacement characters
	out, err := readFile(t, o, pathArg(write("latin1.log", []byte("M\xe4rz ok\nzweite Zeile\n"))))
	if err != nil || !strings.Contains(out["content"].(string), "rz ok") {
		t.Fatalf("%v %v", out, err)
	}
	// an empty file is fine
	if out, err := readFile(t, o, pathArg(write("empty.log", nil))); err != nil || out["content"] != "" {
		t.Fatalf("%v %v", out, err)
	}
}

func TestFilesReadMasksSecretsAndAGrepForTheValueFindsNothing(t *testing.T) {
	root, _ := filesTree(t)
	body := "start\nDB_PASSWORD=hunter2hunter2\nconnect postgres://app:s3cretpw@db.example.lan/x\nkey sk-abcdefghijklmnopqrstuvwx\nend\n"
	p := filepath.Join(root, "conf.log")
	if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	o := filesOpts(root)
	out, err := readFile(t, o, pathArg(p))
	if err != nil {
		t.Fatal(err)
	}
	c := out["content"].(string)
	for _, leak := range []string{"hunter2", "s3cretpw", "abcdefghijklmnopqrstuvwx"} {
		if strings.Contains(c, leak) {
			t.Fatalf("%q leaked: %s", leak, c)
		}
	}
	if !strings.Contains(c, "«masked:") || out["masked"].(float64) < 3 {
		t.Fatalf("nothing was masked: %v", out)
	}
	// grep and tail work on the masked text: the oracle "does this value appear" does not exist
	for _, in := range []string{
		mustJSON(map[string]any{"path": p, "grep": "hunter2"}),
		mustJSON(map[string]any{"path": p, "tail": 10, "grep": "s3cretpw"}),
	} {
		g, err := readFile(t, o, in)
		if err != nil {
			t.Fatal(err)
		}
		if g["content"] != "" || g["matches"].(float64) != 0 {
			t.Fatalf("a grep for a secret value found something: %v", g)
		}
	}
	tail, err := readFile(t, o, mustJSON(map[string]any{"path": p, "tail": 5}))
	if err != nil || strings.Contains(tail["content"].(string), "hunter2") {
		t.Fatalf("%v %v", tail, err)
	}
}

func TestFilesReadPagesAndLimits(t *testing.T) {
	root, _ := filesTree(t)
	var b strings.Builder
	for i := 0; i < 20000; i++ {
		fmt.Fprintf(&b, "line %05d\n", i)
	}
	p := filepath.Join(root, "big.log")
	if err := os.WriteFile(p, []byte(b.String()), 0o600); err != nil {
		t.Fatal(err)
	}
	o := filesOpts(root)
	first, err := readFile(t, o, pathArg(p))
	if err != nil {
		t.Fatal(err)
	}
	c := first["content"].(string)
	if len(c) > FilesPageBytes || !strings.HasSuffix(c, "\n") || first["truncated"] != true {
		t.Fatalf("first page: len=%d truncated=%v", len(c), first["truncated"])
	}
	next := int64(first["next_offset"].(float64))
	if next != int64(len(c)) {
		t.Fatalf("next_offset %d, page %d", next, len(c))
	}
	second, err := readFile(t, o, mustJSON(map[string]any{"path": p, "offset": next}))
	if err != nil || !strings.HasPrefix(second["content"].(string), fmt.Sprintf("line %05d", len(c)/11)) {
		t.Fatalf("second page does not continue: %v %v", second["content"].(string)[:20], err)
	}
	// the whole file by pages, nothing lost or doubled
	var all strings.Builder
	off := int64(0)
	for pages := 0; ; pages++ {
		pg, err := readFile(t, o, mustJSON(map[string]any{"path": p, "offset": off, "limit": FilesMaxPageBytes}))
		if err != nil {
			t.Fatal(err)
		}
		all.WriteString(pg["content"].(string))
		if pg["next_offset"] == nil {
			break
		}
		off = int64(pg["next_offset"].(float64))
		if pages > 50 {
			t.Fatal("does not end")
		}
	}
	if all.String() != b.String() {
		t.Fatalf("pages do not add up to the file: %d vs %d bytes", all.Len(), b.Len())
	}
	// limits
	for _, in := range []string{
		mustJSON(map[string]any{"path": p, "limit": FilesMaxPageBytes + 1}),
		mustJSON(map[string]any{"path": p, "limit": 0}),
		mustJSON(map[string]any{"path": p, "offset": -1}),
		mustJSON(map[string]any{"path": p, "tail": 501}),
		mustJSON(map[string]any{"path": p, "tail": 0}),
		mustJSON(map[string]any{"path": p, "tail": 5, "offset": 10}),
		mustJSON(map[string]any{"path": p, "tail": 5, "limit": 10}),
		mustJSON(map[string]any{"path": p, "extra": 1}),
		`{}`,
	} {
		_, err := readFile(t, o, in)
		wantRefused(t, err, in)
	}
	// an offset past the end is an empty last page
	end, err := readFile(t, o, mustJSON(map[string]any{"path": p, "offset": 1 << 40}))
	if err != nil || end["content"] != "" || end["next_offset"] != nil {
		t.Fatalf("%v %v", end, err)
	}
}

func TestFilesReadTailAndGrep(t *testing.T) {
	root, _ := filesTree(t)
	var b strings.Builder
	for i := 0; i < 30000; i++ {
		fmt.Fprintf(&b, "2026-10-30 12:00:%02d info worker %d ok\n", i%60, i)
		if i%1000 == 999 {
			fmt.Fprintf(&b, "2026-10-30 12:00:00 ERROR worker %d failed\n", i)
		}
	}
	p := filepath.Join(root, "worker.log")
	if err := os.WriteFile(p, []byte(b.String()), 0o600); err != nil {
		t.Fatal(err)
	}
	o := filesOpts(root)
	tail, err := readFile(t, o, mustJSON(map[string]any{"path": p, "tail": 3}))
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Split(tail["content"].(string), "\n"); len(got) != 3 || !strings.Contains(got[1], "worker 29999 ok") || !strings.Contains(got[2], "ERROR worker 29999 failed") {
		t.Fatalf("tail = %q", tail["content"])
	}
	// the window is the end of the file, the answer says it is not all
	if tail["truncated"] != true || tail["window_start"].(float64) <= 0 {
		t.Fatalf("%v", tail)
	}
	errs, err := readFile(t, o, mustJSON(map[string]any{"path": p, "tail": 500, "grep": "error"})) // case-insensitive
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(errs["content"].(string), "\n")
	if len(lines) < 3 || len(lines) > 12 || !strings.Contains(lines[len(lines)-1], "ERROR worker 29999 failed") {
		t.Fatalf("grep over the tail window: %d lines, last %q", len(lines), lines[len(lines)-1])
	}
	// grep over a page: scans up to 256 KiB from the offset and tells where it stopped
	g, err := readFile(t, o, mustJSON(map[string]any{"path": p, "grep": "ERROR"}))
	if err != nil {
		t.Fatal(err)
	}
	if g["truncated"] != true || g["next_offset"] == nil || g["matches"].(float64) < 3 {
		t.Fatalf("%v", g)
	}
	// a plain substring, not a pattern
	none, err := readFile(t, o, mustJSON(map[string]any{"path": p, "tail": 50, "grep": "w.rker"}))
	if err != nil || none["matches"].(float64) != 0 {
		t.Fatalf("grep is not a regular expression: %v %v", none, err)
	}
	// a huge single line never exceeds the output limit
	long := filepath.Join(root, "long.log")
	if err := os.WriteFile(long, []byte(strings.Repeat("x", 300<<10)+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, in := range []string{mustJSON(map[string]any{"path": long, "tail": 1}), mustJSON(map[string]any{"path": long, "limit": FilesMaxPageBytes}), mustJSON(map[string]any{"path": long, "grep": "x"})} {
		out, err := readFile(t, o, in)
		if err != nil {
			t.Fatal(err)
		}
		if n := len(out["content"].(string)); n > FilesMaxPageBytes+200 {
			t.Fatalf("%s: %d bytes", in, n)
		}
	}
}

func TestFilesReadDoesNotSplitACharacter(t *testing.T) {
	root, _ := filesTree(t)
	p := filepath.Join(root, "utf8.log")
	if err := os.WriteFile(p, []byte(strings.Repeat("ä", 30000)), 0o600); err != nil { // no line breaks, 2-byte runes
		t.Fatal(err)
	}
	var got strings.Builder
	off := int64(0)
	for {
		pg, err := readFile(t, filesOpts(root), mustJSON(map[string]any{"path": p, "offset": off}))
		if err != nil {
			t.Fatal(err)
		}
		got.WriteString(pg["content"].(string))
		if pg["next_offset"] == nil {
			break
		}
		off = int64(pg["next_offset"].(float64))
	}
	if got.String() != strings.Repeat("ä", 30000) {
		t.Fatalf("pages split characters (%d bytes back)", got.Len())
	}
}

func TestFilesListPagesAndSorts(t *testing.T) {
	root, _ := filesTree(t)
	for i := 0; i < 7; i++ {
		if err := os.WriteFile(filepath.Join(root, fmt.Sprintf("f%d.log", i)), []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	o := filesOpts(root)
	var seen []string
	after := ""
	for i := 0; i < 10; i++ {
		in := map[string]any{"path": root, "limit": 4}
		if after != "" {
			in["after"] = after
		}
		out, err := invoke(t, o, "files.list", mustJSON(in))
		if err != nil {
			t.Fatal(err)
		}
		for _, e := range out["entries"].([]any) {
			seen = append(seen, e.(map[string]any)["name"].(string))
		}
		if out["truncated"] != true {
			break
		}
		after = out["next_after"].(string)
	}
	if strings.Join(seen, " ") != "app.log f0.log f1.log f2.log f3.log f4.log f5.log f6.log sub" {
		t.Fatalf("seen = %v", seen)
	}
	_, err := invoke(t, o, "files.list", mustJSON(map[string]any{"path": root, "limit": 501}))
	wantRefused(t, err, "limit")
	// a file is no folder, a folder is no file
	_, err = invoke(t, o, "files.list", pathArg(filepath.Join(root, "app.log")))
	wantRefused(t, err, "list a file")
	_, err = readFile(t, o, pathArg(filepath.Join(root, "sub")))
	wantRefused(t, err, "read a folder")
}

func TestFilesSayWhenTheServiceAccountCannotRead(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("a file mode does not lock the owner out on Windows")
	}
	if os.Geteuid() == 0 {
		t.Skip("root reads everything")
	}
	root, _ := filesTree(t)
	p := filepath.Join(root, "locked.log")
	if err := os.WriteFile(p, []byte("x"), 0o000); err != nil {
		t.Fatal(err)
	}
	_, err := readFile(t, filesOpts(root), pathArg(p))
	if err == nil || !strings.Contains(err.Error(), "permission denied") {
		t.Fatalf("want a permission message, got %v", err)
	}
	// a path that does not exist inside a root says so; outside it says nothing about existence
	_, err = readFile(t, filesOpts(root), pathArg(filepath.Join(root, "nope.log")))
	if err == nil || !strings.Contains(err.Error(), "no such file") {
		t.Fatalf("%v", err)
	}
	_, err = readFile(t, filesOpts(root), pathArg("/nonexistent/place"))
	if err == nil || strings.Contains(err.Error(), "no such file") {
		t.Fatalf("outside a root the answer must not tell whether it exists: %v", err)
	}
}

func TestFilesAnAbsentPathInsideARootButNotOutsideIsExplained(t *testing.T) {
	root, _ := filesTree(t)
	_, err := readFile(t, filesOpts(root), pathArg(filepath.Join(root, "nope.log")))
	if err == nil || !strings.Contains(err.Error(), "does not exist") {
		t.Fatalf("%v", err)
	}
	_, err = readFile(t, filesOpts(root), pathArg("/nonexistent/place"))
	if err == nil || strings.Contains(err.Error(), "no such file") {
		t.Fatalf("outside a root the answer must not tell whether it exists: %v", err)
	}
}

func TestFilesRootsMustBeAcceptable(t *testing.T) {
	root, _ := filesTree(t)
	// a root that is a filesystem root or invalid releases nothing, even if it slips into the options
	for _, bad := range []string{"/", "relative/dir", "//server/share", "/../etc"} {
		_, err := readFile(t, Options{ReadRoots: []string{bad}}, pathArg("/etc/hostname"))
		wantRefused(t, err, "root "+bad)
	}
	// but one good root among bad ones still works
	if _, err := readFile(t, Options{ReadRoots: []string{"/", root}}, pathArg(filepath.Join(root, "app.log"))); err != nil {
		t.Fatal(err)
	}
	_, err := readFile(t, Options{ReadRoots: []string{"/", root}}, pathArg("/etc/hostname"))
	wantRefused(t, err, "outside with a bad root listed")
}
