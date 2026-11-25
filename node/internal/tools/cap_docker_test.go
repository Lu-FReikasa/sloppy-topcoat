package tools

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

const psList = `[
 {"Id":"aaaaaaaaaaaaffff","Names":["/web"],"Image":"nginx:1","State":"running","Status":"Up 3 days (healthy)","Labels":{"com.docker.compose.project":"site","com.docker.compose.service":"web"},"Ports":[{"PrivatePort":80,"PublicPort":8080,"Type":"tcp"},{"PrivatePort":80,"PublicPort":8080,"Type":"tcp"}]},
 {"Id":"bbbbbbbbbbbbffff","Names":["/db"],"Image":"postgres:16","State":"exited","Status":"Exited (137) 2 hours ago","Labels":{"com.docker.compose.project":"site","com.docker.compose.service":"db"}}
]`

func dockerWithTwoContainers(t *testing.T) *fakeDocker {
	f := newFakeDocker(t)
	f.list = psList
	f.inspect["aaaaaaaaaaaaffff"] = `{"Id":"aaaaaaaaaaaaffff","Name":"/web","Config":{"Image":"nginx:1","Tty":false},"State":{"Status":"running","Running":true,"StartedAt":"2026-09-27T10:00:00Z","FinishedAt":"0001-01-01T00:00:00Z","Health":{"Status":"healthy","FailingStreak":0}},"RestartCount":0}`
	f.inspect["bbbbbbbbbbbbffff"] = `{"Id":"bbbbbbbbbbbbffff","Name":"/db","Config":{"Image":"postgres:16","Tty":false},"State":{"Status":"exited","Running":false,"OOMKilled":true,"ExitCode":137,"StartedAt":"2026-09-27T10:00:00Z","FinishedAt":"2026-09-30T08:00:00Z"},"RestartCount":4}`
	f.inspect["db"] = f.inspect["bbbbbbbbbbbbffff"]
	f.inspect["web"] = f.inspect["aaaaaaaaaaaaffff"]
	return f
}

func TestDockerPSShowsStateHealthRestartsAndExitCode(t *testing.T) {
	f := dockerWithTwoContainers(t)
	out, err := invoke(t, Options{Docker: f.client()}, "docker.ps", `{"detail":true}`)
	if err != nil {
		t.Fatal(err)
	}
	cs := out["containers"].([]any)
	if len(cs) != 2 {
		t.Fatalf("%v", out)
	}
	db, web := cs[0].(map[string]any), cs[1].(map[string]any) // sorted by name
	if db["name"] != "db" || db["state"] != "exited" || db["exit_code"] != float64(137) || db["oom_killed"] != true || db["restart_count"] != float64(4) {
		t.Fatalf("db: %v", db)
	}
	if web["health"] != "healthy" || web["compose_project"] != "site" || len(web["ports"].([]any)) != 1 || web["id"] != "aaaaaaaaaaaa" {
		t.Fatalf("web: %v", web)
	}
	if _, has := web["exit_code"]; has {
		t.Fatal("a running container has no exit code")
	}
	if db["finished_at"] != "2026-09-30T08:00:00Z" || web["finished_at"] != nil {
		t.Fatalf("the never-stopped timestamp must be hidden: %v", web)
	}
	out, _ = invoke(t, Options{Docker: f.client()}, "docker.ps", `{"name":"WE","all":false}`)
	if cs := out["containers"].([]any); len(cs) != 1 {
		t.Fatalf("name filter: %v", out)
	}
}

// The default is a compact list (first hardware test: 21 containers were 7 KB, "cut for the
// prompt", in a 16K context): name, state, health, restarts, start; exit code and OOM kill only
// where a container is not running. Details are one parameter away.
func TestDockerPSIsCompactByDefault(t *testing.T) {
	f := dockerWithTwoContainers(t)
	out, err := invoke(t, Options{Docker: f.client()}, "docker.ps", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	if out["compact"] != true || out["total"] != float64(2) {
		t.Fatalf("%v", out)
	}
	counts := out["counts"].(map[string]any)
	if counts["running"] != float64(1) || counts["exited"] != float64(1) {
		t.Fatalf("counts %v", counts)
	}
	cs := out["containers"].([]any)
	db, web := cs[0].(map[string]any), cs[1].(map[string]any)
	if db["name"] != "db" || db["state"] != "exited" || db["exit_code"] != float64(137) || db["oom_killed"] != true || db["restart_count"] != float64(4) {
		t.Fatalf("db: %v", db)
	}
	if web["name"] != "web" || web["health"] != "healthy" || web["started_at"] != "2026-09-27T10:00:00Z" || web["restart_count"] != float64(0) {
		t.Fatalf("web: %v", web)
	}
	for _, key := range []string{"image", "id", "status", "ports", "compose_project", "compose_service", "finished_at"} {
		if _, has := web[key]; has {
			t.Errorf("the compact list has %q", key)
		}
		if _, has := db[key]; has {
			t.Errorf("the compact list has %q", key)
		}
	}
	if _, has := web["exit_code"]; has {
		t.Fatal("a running container has no exit code")
	}
	full, _ := invoke(t, Options{Docker: f.client()}, "docker.ps", `{"detail":true}`)
	if _, has := full["compact"]; has || full["containers"].([]any)[1].(map[string]any)["image"] == nil {
		t.Fatalf("detail=true must give the full list: %v", full)
	}
	if _, err := invoke(t, Options{Docker: f.client()}, "docker.ps", `{"detail":"yes"}`); err == nil {
		t.Fatal("detail must be a boolean")
	}
}

func TestDockerPSOf21ContainersFitsASmallContext(t *testing.T) {
	f := newFakeDocker(t)
	var list []string
	for i := 0; i < 21; i++ {
		id := fmt.Sprintf("%012x%04x", i+1, 0xffff)
		name := fmt.Sprintf("stack-service-%02d-1", i)
		list = append(list, fmt.Sprintf(`{"Id":%q,"Names":["/%s"],"Image":"registry.example.lan/team/service-%02d:1.2.3","State":"running","Status":"Up 3 days (healthy)","Ports":[{"PrivatePort":8080,"PublicPort":%d,"Type":"tcp"}],"Labels":{"com.docker.compose.project":"stack","com.docker.compose.service":"service-%02d"}}`, id, name, i, 8000+i, i))
		f.inspect[id] = fmt.Sprintf(`{"Id":%q,"Name":"/%s","Config":{"Image":"x","Tty":false},"State":{"Status":"running","Running":true,"StartedAt":"2026-09-27T10:00:00Z","Health":{"Status":"healthy"}},"RestartCount":0}`, id, name)
	}
	f.list = "[" + strings.Join(list, ",") + "]"
	o := Options{Docker: f.client()}
	compact, err := invoke(t, o, "docker.ps", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	full, _ := invoke(t, o, "docker.ps", `{"detail":true}`)
	cb, _ := json.Marshal(compact)
	fb, _ := json.Marshal(full)
	if len(cb) > 3500 || len(cb)*2 > len(fb) {
		t.Fatalf("compact %d bytes, full %d bytes", len(cb), len(fb))
	}
}

func TestTheDockerClientOnlyEverSendsGET(t *testing.T) {
	f := dockerWithTwoContainers(t)
	f.logs["db"] = frames([2]string{"out", "2026-09-30T07:59:00Z hello\n"})
	o := Options{Docker: f.client(), Commander: &fakeCommander{}}
	for _, c := range []struct{ name, in string }{
		{"docker.ps", `{}`}, {"docker.inspect", `{"container":"db"}`}, {"docker.logs", `{"container":"db"}`}, {"compose.projects", `{}`},
	} {
		if _, err := invoke(t, o, c.name, c.in); err != nil {
			t.Fatalf("%s: %v", c.name, err)
		}
	}
	for _, m := range f.methods {
		if m != "GET" {
			t.Fatalf("a %s request reached the Docker API", m)
		}
	}
	// and the client itself refuses everything outside its list
	d := f.client()
	for _, p := range []string{"/containers/create", "/containers/db/restart", "/containers/db/exec", "/containers/../etc/json", "/images/json", "/containers/db/json/../../x", "/v1.41/containers/json"} {
		if _, _, err := d.Get(t.Context(), p, nil); err == nil || !strings.Contains(err.Error(), "not one this node reads") {
			t.Errorf("%s: %v", p, err)
		}
	}
}

func TestDockerHostMustBeASocketOrLoopback(t *testing.T) {
	for _, h := range []string{"tcp://10.0.0.5:2375", "tcp://docker.example.lan:2375", "http://127.0.0.1:2375", "ssh://root@host", "unix://"} {
		if _, err := NewDocker(h); err == nil {
			t.Errorf("%q accepted", h)
		}
	}
	for _, h := range []string{"", "unix:///var/run/docker.sock", "tcp://127.0.0.1:2375", "tcp://localhost:2375", "tcp://[::1]:2375"} {
		if _, err := NewDocker(h); err != nil {
			t.Errorf("%q: %v", h, err)
		}
	}
}

func TestDockerLogsDemuxMaskAndLimit(t *testing.T) {
	f := dockerWithTwoContainers(t)
	f.logs["db"] = frames(
		[2]string{"out", "2026-09-30T07:59:00Z starting\n"},
		[2]string{"err", "2026-09-30T07:59:01Z connect postgres://app:pa55w0rd@db/app failed\n"},
		[2]string{"out", "2026-09-30T07:59:02Z ready\n"},
	)
	now := mustTime("2026-09-30T08:00:00Z")
	out, err := invoke(t, Options{Docker: f.client(), Now: func() time.Time { return now }}, "docker.logs", `{"container":"db","lines":50,"since":"10m","until":"2026-09-30T07:59:30Z"}`)
	if err != nil {
		t.Fatal(err)
	}
	log := out["log"].(string)
	if !strings.Contains(log, "starting\n") || !strings.Contains(log, "ready") || strings.Contains(log, "pa55w0rd") || !strings.Contains(log, "«masked:url_credentials»") {
		t.Fatalf("%q", log)
	}
	if out["lines"] != float64(3) || out["masked"] != float64(1) || out["truncated"] != false || out["container"] != "db" {
		t.Fatalf("%v", out)
	}
	q := f.queries[len(f.queries)-1]
	for _, want := range []string{"tail=50", "stdout=1", "stderr=1", "timestamps=1", fmt.Sprintf("since=%d", now.Add(-10*time.Minute).Unix()), fmt.Sprintf("until=%d", mustTime("2026-09-30T07:59:30Z").Unix())} {
		if !strings.Contains(q, want) {
			t.Errorf("query %q lacks %s", q, want)
		}
	}
}

func TestDockerLogsForATTYContainerAreRaw(t *testing.T) {
	f := newFakeDocker(t)
	f.inspect["tty"] = `{"Id":"cccc","Name":"/tty","Config":{"Tty":true},"State":{}}`
	f.logs["tty"] = []byte("plain line 1\nplain line 2\n")
	out, err := invoke(t, Options{Docker: f.client()}, "docker.logs", `{"container":"tty"}`)
	if err != nil || out["log"] != "plain line 1\nplain line 2" {
		t.Fatalf("%v %v", out, err)
	}
}

func TestDockerLogsKeepTheNewestPartOfAHugeLog(t *testing.T) {
	f := newFakeDocker(t)
	f.inspect["big"] = `{"Id":"dddd","Name":"/big","Config":{},"State":{}}`
	var b strings.Builder
	for i := 0; i < 5000; i++ {
		b.WriteString("2026-09-30T07:59:00Z line number ")
		b.WriteString(strings.Repeat("x", 20))
		b.WriteString("\n")
	}
	f.logs["big"] = frames([2]string{"out", b.String()})
	out, err := invoke(t, Options{Docker: f.client()}, "docker.logs", `{"container":"big","lines":500}`)
	if err != nil {
		t.Fatal(err)
	}
	log := out["log"].(string)
	if out["truncated"] != true || len(log) > DefaultMaxOutputBytes+100 || !strings.HasPrefix(log, "[… ") {
		t.Fatalf("truncated=%v len=%d head=%q", out["truncated"], len(log), log[:40])
	}
}

func TestDockerInspectReturnsNoEnvironmentAndMasksTheRest(t *testing.T) {
	f := newFakeDocker(t)
	f.inspect["app"] = `{"Id":"eeeeeeeeeeeeeeee","Name":"/app","Path":"/bin/app","Args":["--password=topsecret123","--port=80"],
	 "Config":{"Image":"app:1","Env":["DB_PASSWORD=hunter2hunter2"],"Labels":{"com.docker.compose.project":"site","com.docker.compose.service":"app"}},
	 "State":{"Status":"running","Running":true,"Error":"","Health":{"Status":"unhealthy","FailingStreak":5,"Log":[{"End":"t","ExitCode":1,"Output":"curl: (22) Bearer abcdefghijklmnopqrstuvwxyz rejected"}]}},
	 "HostConfig":{"RestartPolicy":{"Name":"unless-stopped"}},
	 "Mounts":[{"Type":"bind","Source":"/srv/app","Destination":"/data","RW":true}],
	 "NetworkSettings":{"Networks":{"site_default":{}}}}`
	out, err := invoke(t, Options{Docker: f.client()}, "docker.inspect", `{"container":"app"}`)
	if err != nil {
		t.Fatal(err)
	}
	raw := mustJSON(out)
	for _, leak := range []string{"hunter2hunter2", "topsecret123", "abcdefghijklmnopqrstuvwxyz", "DB_PASSWORD"} {
		if strings.Contains(raw, leak) {
			t.Errorf("%q leaked: %s", leak, raw)
		}
	}
	for _, want := range []string{`"restart_policy":"unless-stopped"`, `"failing_streak":5`, `"destination":"/data"`, `"site_default"`, `--port=80`} {
		if !strings.Contains(raw, want) {
			t.Errorf("%q missing: %s", want, raw)
		}
	}
}

func TestDockerInputsAreChecked(t *testing.T) {
	f := dockerWithTwoContainers(t)
	o := Options{Docker: f.client()}
	for name, c := range map[string]struct{ cap, in string }{
		"path in container":  {"docker.logs", `{"container":"../etc"}`},
		"slash":              {"docker.inspect", `{"container":"a/json"}`},
		"query injection":    {"docker.logs", `{"container":"db?all=1"}`},
		"too many lines":     {"docker.logs", `{"container":"db","lines":501}`},
		"negative lines":     {"docker.logs", `{"container":"db","lines":-1}`},
		"unknown field":      {"docker.logs", `{"container":"db","follow":true}`},
		"bad duration":       {"docker.logs", `{"container":"db","since":"yesterday"}`},
		"until before since": {"docker.logs", `{"container":"db","since":"1h","until":"2h"}`},
		"missing container":  {"docker.logs", `{}`},
		"array instead":      {"docker.ps", `[1]`},
		"unknown ps field":   {"docker.ps", `{"sh":"id"}`},
	} {
		var ie *InputError
		if _, err := invoke(t, o, c.cap, c.in); !errors.As(err, &ie) {
			t.Errorf("%s: want an input error, got %v", name, err)
		}
	}
	before := len(f.paths)
	_, _ = invoke(t, o, "docker.logs", `{"container":"nope"}`)
	var ie *InputError
	if _, err := invoke(t, o, "docker.inspect", `{"container":"nope"}`); !errors.As(err, &ie) || !strings.Contains(err.Error(), "no such container") {
		t.Errorf("unknown container: %v", err)
	}
	_ = before
}

func TestWithoutADockerAPIDockerCapabilitiesSayWhy(t *testing.T) {
	for _, c := range []string{"docker.ps", "compose.projects"} {
		if _, err := invoke(t, Options{}, c, `{}`); err == nil || !strings.Contains(err.Error(), "docker_host") {
			t.Errorf("%s: %v", c, err)
		}
	}
}
