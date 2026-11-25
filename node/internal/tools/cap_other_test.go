package tools

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/providers/capability"
)

// composeHost is a Docker host with one compose project whose files live in a temp directory.
func composeHost(t *testing.T) (*fakeDocker, string) {
	t.Helper()
	dir := t.TempDir()
	write := func(name, content string) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
		return p
	}
	compose := write("compose.yaml", "services:\n  db:\n    image: postgres:16\n    environment:\n      POSTGRES_PASSWORD: hunter2hunter2\n      TZ: Europe/Berlin\n")
	write(".env", "DB_PASSWORD=s3cr3t-value\nHOST=db\n")
	write("notes.txt", "not a compose file")
	f := newFakeDocker(t)
	f.list = fmt.Sprintf(`[{"Id":"aaaaaaaaaaaaffff","Names":["/site-db-1"],"Image":"postgres:16","State":"running","Status":"Up","Labels":{"com.docker.compose.project":"site","com.docker.compose.service":"db","com.docker.compose.project.working_dir":%q,"com.docker.compose.project.config_files":%q}}]`, dir, compose)
	return f, dir
}

func TestComposeProjectsListFilesAndServices(t *testing.T) {
	f, dir := composeHost(t)
	out, err := invoke(t, Options{Docker: f.client(), ComposeRoots: []string{dir}}, "compose.projects", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	p := out["projects"].([]any)[0].(map[string]any)
	if p["project"] != "site" || p["working_dir"] != dir || p["running"] != float64(1) || p["files_released"] != true {
		t.Fatalf("%v", p)
	}
	if files := p["config_files"].([]any); len(files) != 1 || files[0] != filepath.Join(dir, "compose.yaml") {
		t.Fatalf("%v", files)
	}
}

// Default deny: a node without compose_roots reads and lists no compose or .env file, and
// compose.projects keeps the paths to itself (the first hardware test found a node that read every
// project's .env without anybody having released a directory).
// absInput is an input naming the project's compose file by its absolute path, as JSON (a Windows
// path has backslashes that must be escaped).
func absInput(dir string) string {
	b, _ := json.Marshal(map[string]string{"project": "site", "file": filepath.Join(dir, "compose.yaml")})
	return string(b)
}

func TestComposeWithoutRootsIsDenied(t *testing.T) {
	f, dir := composeHost(t)
	for _, roots := range [][]string{nil, {}} {
		o := Options{Docker: f.client(), ComposeRoots: roots}
		for _, in := range []string{`{"project":"site"}`, `{"project":"site","file":".env"}`, `{"project":"site","file":"compose.yaml"}`, absInput(dir)} {
			var ie *InputError
			_, err := invoke(t, o, "compose.file", in)
			if !errors.As(err, &ie) || !strings.Contains(err.Error(), "no directories are released") || !strings.Contains(err.Error(), "compose_roots") {
				t.Errorf("%s: want the refusal about compose_roots, got %v", in, err)
			}
		}
		if files := o.composeFilesIn(dir); len(files) != 0 {
			t.Errorf("a directory that is not released was listed: %v", files)
		}
		out, err := invoke(t, o, "compose.projects", `{}`)
		if err != nil {
			t.Fatal(err)
		}
		p := out["projects"].([]any)[0].(map[string]any)
		if p["project"] != "site" || p["running"] != float64(1) || len(p["services"].([]any)) != 1 {
			t.Fatalf("names, services and state stay visible: %v", p)
		}
		for _, k := range []string{"working_dir", "config_files"} {
			if _, has := p[k]; has {
				t.Errorf("%s is shown although nothing is released: %v", k, p)
			}
		}
		if p["files_released"] != false || out["compose_roots_set"] != false {
			t.Errorf("%v", out)
		}
		if strings.Contains(mustJSON(out), dir) {
			t.Errorf("the directory leaked: %s", mustJSON(out))
		}
	}
}

func TestComposeProjectsShowPathsOnlyInsideTheRoots(t *testing.T) {
	f, dir := composeHost(t)
	out, err := invoke(t, Options{Docker: f.client(), ComposeRoots: []string{t.TempDir()}}, "compose.projects", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	p := out["projects"].([]any)[0].(map[string]any)
	if _, has := p["working_dir"]; has || p["files_released"] != false || out["compose_roots_set"] != true {
		t.Fatalf("%v", out)
	}
	if strings.Contains(mustJSON(out), dir) {
		t.Fatalf("the directory leaked: %s", mustJSON(out))
	}
}

func TestComposeFileMasksSecretsAndOffersTheOthers(t *testing.T) {
	f, dir := composeHost(t)
	o := Options{Docker: f.client(), ComposeRoots: []string{dir}}
	out, err := invoke(t, o, "compose.file", `{"project":"site"}`)
	if err != nil {
		t.Fatal(err)
	}
	c := out["content"].(string)
	if strings.Contains(c, "hunter2hunter2") || !strings.Contains(c, "POSTGRES_PASSWORD: «masked:secret_key»") || !strings.Contains(c, "TZ: Europe/Berlin") {
		t.Fatalf("%s", c)
	}
	if out["masked"] != float64(1) {
		t.Fatalf("%v", out["masked"])
	}
	av := mustJSON(out["available"])
	if !strings.Contains(av, ".env") || !strings.Contains(av, "compose.yaml") || strings.Contains(av, "notes.txt") {
		t.Fatalf("available: %s", av)
	}
	out, err = invoke(t, o, "compose.file", `{"project":"site","file":".env"}`)
	if err != nil {
		t.Fatal(err)
	}
	if c := out["content"].(string); strings.Contains(c, "s3cr3t-value") || !strings.Contains(c, "HOST=db") {
		t.Fatalf("%s", c)
	}
}

func TestComposeFileReadsNothingElse(t *testing.T) {
	f, dir := composeHost(t)
	outside := filepath.Join(t.TempDir(), "outside.env")
	if err := os.WriteFile(outside, []byte("TOKEN=outsider123456\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(dir, "link.env")); err != nil {
		t.Skip("symbolic links need a privilege on Windows: ", err)
	}
	if err := os.Symlink("/etc/passwd", filepath.Join(dir, "compose.override.yaml")); err != nil {
		t.Skip("symbolic links need a privilege on Windows: ", err)
	}
	o := Options{Docker: f.client(), ComposeRoots: []string{dir}}
	for name, file := range map[string]string{
		"a text file":           "notes.txt",
		"an absolute path":      "/etc/passwd",
		"a parent path":         "../../etc/passwd",
		"a sub path":            "sub/.env",
		"the node key":          "node.key",
		"a link out (env)":      "link.env",
		"a link out (compose)":  "compose.override.yaml",
		"a missing file":        ".env.local",
		"shell metacharacters":  ".env;id",
		"a directory traversal": "..",
	} {
		var ie *InputError
		_, err := invoke(t, o, "compose.file", fmt.Sprintf(`{"project":"site","file":%q}`, file))
		if !errors.As(err, &ie) {
			t.Errorf("%s: want an input error, got %v", name, err)
		}
	}
	if _, err := invoke(t, o, "compose.file", `{"project":"nope"}`); err == nil {
		t.Error("unknown project accepted")
	}
	if _, err := invoke(t, o, "compose.file", `{"project":"../x"}`); err == nil {
		t.Error("bad project name accepted")
	}
}

func TestALabelCannotPointComposeFileAtAnArbitraryFile(t *testing.T) {
	f := newFakeDocker(t)
	f.list = `[{"Id":"aaaaaaaaaaaaffff","Names":["/evil"],"State":"running","Labels":{"com.docker.compose.project":"evil","com.docker.compose.project.working_dir":"/var/lib/topcoat-node","com.docker.compose.project.config_files":"/var/lib/topcoat-node/node.key,/etc/shadow"}}]`
	// even with the whole machine released a label cannot point the read at the node's own files
	o := Options{Docker: f.client(), ComposeRoots: []string{"/"}}
	for _, in := range []string{`{"project":"evil"}`, `{"project":"evil","file":"/etc/shadow"}`, `{"project":"evil","file":"node.key"}`} {
		if _, err := invoke(t, o, "compose.file", in); err == nil {
			t.Errorf("%s was read", in)
		}
	}
}

func TestComposeRootsRestrictWhereFilesAreRead(t *testing.T) {
	f, dir := composeHost(t)
	o := Options{Docker: f.client(), ComposeRoots: []string{t.TempDir()}}
	if _, err := invoke(t, o, "compose.file", `{"project":"site"}`); err == nil || !strings.Contains(err.Error(), "compose_roots") {
		t.Fatalf("%v", err)
	}
	o.ComposeRoots = []string{filepath.Dir(dir)}
	if _, err := invoke(t, o, "compose.file", `{"project":"site"}`); err != nil {
		t.Fatalf("%v", err)
	}
}

func TestJournalLogsBuildAFixedCommandAndMask(t *testing.T) {
	c := &fakeCommander{out: map[string]string{"journalctl": "2026-09-30T07:00:00+0000 host app[1]: started\n2026-09-30T07:00:01+0000 host app[1]: login password=letmein123\n"}}
	now := mustTime("2026-09-30T08:00:00Z")
	out, err := invoke(t, Options{Commander: c, Now: func() time.Time { return now }}, "journal.logs", `{"unit":"docker.service","lines":20,"since":"2h","priority":3}`)
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(c.calls[0], " "); got != "journalctl --no-pager -q -o short-iso -u docker.service -n 20 --since 2026-09-30 06:00:00 UTC -p 3" {
		t.Fatalf("%s", got)
	}
	log := out["log"].(string)
	if strings.Contains(log, "letmein123") || !strings.Contains(log, "started") || out["lines"] != float64(2) {
		t.Fatalf("%v", out)
	}
}

func TestServiceStatusReportsAMissingUnit(t *testing.T) {
	c := &fakeCommander{out: map[string]string{
		"systemctl show": "Id=nope.service\nDescription=nope.service\nLoadState=not-found\nActiveState=inactive\nSubState=dead\n",
	}}
	_, err := invoke(t, Options{Commander: c}, "service.status", `{"unit":"nope.service"}`)
	if err == nil || !strings.Contains(err.Error(), "not found") || !strings.Contains(err.Error(), "nope.service") {
		t.Fatalf("a missing unit must be reported as not found, got %v", err)
	}
	var ie *InputError
	if !errors.As(err, &ie) {
		t.Fatalf("want an input error (invalid_args), got %T", err)
	}
}

func TestServiceStatusAndFailed(t *testing.T) {
	c := &fakeCommander{out: map[string]string{
		"systemctl show":       "Id=nginx.service\nActiveState=failed\nSubState=failed\nResult=exit-code\nExecMainStatus=1\nNRestarts=5\nMemoryCurrent=[not set]\nDescription=\n",
		"systemctl list-units": "nginx.service loaded failed failed A high performance web server\nbackup.timer loaded failed failed Nightly backup\n",
	}}
	o := Options{Commander: c}
	out, err := invoke(t, o, "service.status", `{"unit":"nginx.service"}`)
	if err != nil {
		t.Fatal(err)
	}
	props := out["properties"].(map[string]any)
	if props["ActiveState"] != "failed" || props["NRestarts"] != "5" || props["Result"] != "exit-code" {
		t.Fatalf("%v", props)
	}
	if _, has := props["MemoryCurrent"]; has {
		t.Fatal("[not set] values must be left out")
	}
	if got := strings.Join(c.calls[0], " "); !strings.HasPrefix(got, "systemctl show --no-pager --property=Id,") || !strings.HasSuffix(got, " nginx.service") {
		t.Fatalf("%s", got)
	}
	out, err = invoke(t, o, "service.failed", `{}`)
	if err != nil || out["count"] != float64(2) {
		t.Fatalf("%v %v", out, err)
	}
	if u := out["failed"].([]any)[0].(map[string]any); u["unit"] != "nginx.service" || u["description"] != "A high performance web server" {
		t.Fatalf("%v", u)
	}
}

func TestSystemdInputsCannotInjectArguments(t *testing.T) {
	c := &fakeCommander{}
	o := Options{Commander: c}
	for _, unit := range []string{"--help", "-u", "x; rm -rf /", "$(id)", "a b", "../../etc", "`id`", "a\nb", "", strings.Repeat("a", 300), "x|y", "x&y"} {
		for _, cp := range []string{"journal.logs", "service.status"} {
			var ie *InputError
			if _, err := invoke(t, o, cp, fmt.Sprintf(`{"unit":%q}`, unit)); !errors.As(err, &ie) {
				t.Errorf("%s accepted unit %q (%v)", cp, unit, err)
			}
		}
	}
	for _, in := range []string{`{"unit":"a.service","lines":0,"priority":8}`, `{"unit":"a.service","priority":-1}`, `{"unit":"a.service","since":"1h","until":"2h"}`, `{"unit":"a.service","extra":1}`} {
		if _, err := invoke(t, o, "journal.logs", in); err == nil {
			t.Errorf("%s accepted", in)
		}
	}
	if c.called() != 0 {
		t.Fatalf("a command ran for refused input: %v", c.calls)
	}
}

func TestSystemInfoReadsProc(t *testing.T) {
	proc, etc := t.TempDir(), t.TempDir()
	put := func(root, name, text string) {
		p := filepath.Join(root, name)
		_ = os.MkdirAll(filepath.Dir(p), 0o755)
		if err := os.WriteFile(p, []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	put(proc, "uptime", "93784.12 350000.00\n")
	put(proc, "loadavg", "0.52 0.41 0.30 2/345 12345\n")
	put(proc, "meminfo", "MemTotal:        8000000 kB\nMemAvailable:    2000000 kB\nSwapTotal:       1024000 kB\nSwapFree:         24000 kB\n")
	put(proc, "sys/kernel/osrelease", "6.8.0-generic\n")
	put(etc, "os-release", "NAME=\"Ubuntu\"\nPRETTY_NAME=\"Ubuntu 24.04.1 LTS\"\n")
	out, err := invoke(t, Options{ProcRoot: proc, EtcRoot: etc}, "system.info", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	m := out["memory_mb"].(map[string]any)
	if out["os"] != "Ubuntu 24.04.1 LTS" || out["kernel"] != "6.8.0-generic" || out["uptime_seconds"] != float64(93784) ||
		out["load"].(map[string]any)["5m"] != 0.41 || m["total"] != float64(7812) || m["available"] != float64(1953) || m["swap_used"] != float64(977) {
		t.Fatalf("%v", out)
	}
}

func TestDiskUsageSkipsPseudoFilesystemsAndSortsByFullness(t *testing.T) {
	proc := t.TempDir()
	_ = os.MkdirAll(filepath.Join(proc, "self"), 0o755)
	mounts := "proc /proc proc rw 0 0\n/dev/sda1 / ext4 rw 0 0\noverlay /var/lib/docker/overlay2/x/merged overlay rw 0 0\n/dev/sdb1 /srv/data ext4 rw 0 0\ntmpfs /run tmpfs rw 0 0\n/dev/sdb1 /mnt/with\\040space ext4 rw 0 0\n"
	if err := os.WriteFile(filepath.Join(proc, "self/mounts"), []byte(mounts), 0o600); err != nil {
		t.Fatal(err)
	}
	stat := func(p string) (FSStat, error) {
		switch p {
		case "/":
			return FSStat{TotalBytes: 100 << 30, FreeBytes: 60 << 30, AvailBytes: 60 << 30, Inodes: 1000, InodesFree: 900}, nil
		case "/srv/data":
			return FSStat{TotalBytes: 200 << 30, FreeBytes: 10 << 30, AvailBytes: 10 << 30, Inodes: 1000, InodesFree: 100}, nil
		}
		return FSStat{}, errors.New("no")
	}
	out, err := invoke(t, Options{ProcRoot: proc, StatFS: stat}, "disk.usage", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	fs := out["filesystems"].([]any)
	if len(fs) != 2 {
		t.Fatalf("%v", fs)
	}
	first := fs[0].(map[string]any)
	if first["mount"] != "/srv/data" || first["used_percent"] != 95.0 || first["inodes_used_percent"] != 90.0 || fs[1].(map[string]any)["mount"] != "/" {
		t.Fatalf("%v", fs)
	}
}

// Every capability is read-only, described and has a strict schema; none takes a command.
func TestEveryCapabilityIsReadOnlyDescribedAndStrict(t *testing.T) {
	// with targets configured, so the collecting capabilities that need them are checked too
	r := NewRegistry(Options{Checks: CheckOptions{CertTargets: []string{"proxy.example.lan:443"}, DNSNames: []string{"example.org"}}})
	want := []string{"certs.expiry", "compose.file", "compose.logs", "compose.projects", "disk.usage", "docker.inspect", "docker.logs", "docker.ps", "health.check", "inventory.snapshot", "journal.logs", "logs.search", "net.check", "service.failed", "service.status", "system.info", "system.memory"}
	got := r.Names()
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("capabilities: %v", got)
	}
	for _, n := range got {
		c, _ := r.Get(n)
		info := capability.InfoOf(c)
		if !info.ReadOnly || len(info.Description) < 30 {
			t.Errorf("%s: %+v", n, info)
		}
		s := string(c.InputSchema())
		if !strings.Contains(s, `"additionalProperties":false`) {
			t.Errorf("%s: the schema accepts unknown fields", n)
		}
		for _, bad := range []string{"command", "cmd", "shell", "exec", "args", "script"} {
			if strings.Contains(strings.ToLower(s), `"`+bad+`"`) {
				t.Errorf("%s: the schema has an input called %q", n, bad)
			}
		}
	}
}

func TestOnlyFixedBinariesAreEverStarted(t *testing.T) {
	c := &fakeCommander{}
	o := Options{Commander: c, Docker: newFakeDocker(t).client()}
	_, _ = invoke(t, o, "journal.logs", `{"unit":"a.service"}`)
	_, _ = invoke(t, o, "service.status", `{"unit":"a.service"}`)
	_, _ = invoke(t, o, "service.failed", `{}`)
	for _, call := range c.calls {
		if call[0] != "journalctl" && call[0] != "systemctl" {
			t.Fatalf("started %s", call[0])
		}
		for _, a := range call[1:] {
			if strings.ContainsAny(a, ";|&$`\n") {
				t.Fatalf("a shell metacharacter reached %v", call)
			}
		}
	}
}

// The patterns in the schemas (the hub checks input against them before it calls the node) and the
// validators in the code (the node checks again) must accept and refuse the same names.
func TestSchemaPatternsAgreeWithTheValidators(t *testing.T) {
	r := NewRegistry(Options{})
	pattern := func(cap, field string) *regexp.Regexp {
		c, _ := r.Get(cap)
		var s struct {
			Properties map[string]struct {
				Pattern string `json:"pattern"`
			} `json:"properties"`
		}
		if err := json.Unmarshal(c.InputSchema(), &s); err != nil {
			t.Fatal(err)
		}
		p := s.Properties[field].Pattern
		if p == "" {
			t.Fatalf("%s.%s has no pattern", cap, field)
		}
		return regexp.MustCompile(p)
	}
	samples := []string{
		"web", "site-db-1", "a.b_c", "Web1", "0abc", "nginx.service", "user@1000.service", `dev-sda1\x2dfoo.mount`,
		"docker:x.service", "", "-u", "--help", "../etc", "a/b", "a b", "a;b", "a|b", "$(id)", "`id`", "a\nb",
		".hidden", "_x", strings.Repeat("a", 129),
	}
	for _, c := range []struct {
		cap, field string
		check      func(string) bool
	}{
		{"docker.logs", "container", func(s string) bool { return checkContainer(s) == nil }},
		{"docker.inspect", "container", func(s string) bool { return checkContainer(s) == nil }},
		{"journal.logs", "unit", func(s string) bool { return checkUnit(s) == nil }},
		{"service.status", "unit", func(s string) bool { return checkUnit(s) == nil }},
		{"compose.file", "project", func(s string) bool { return projectName.MatchString(s) }},
		{"compose.logs", "project", func(s string) bool { return projectName.MatchString(s) }},
		{"compose.logs", "service", func(s string) bool { return projectName.MatchString(s) }},
	} {
		re := pattern(c.cap, c.field)
		for _, s := range samples {
			if len(s) > 128 && c.field != "unit" { // maxLength is the schema's other half
				continue
			}
			if got, want := re.MatchString(s), c.check(s); got != want {
				t.Errorf("%s.%s %q: schema pattern says %v, the validator %v", c.cap, c.field, s, got, want)
			}
		}
	}
}
