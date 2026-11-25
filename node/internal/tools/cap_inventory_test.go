package tools

import (
	"errors"
	"strings"
	"testing"
)

const unitsOut = `app.service            loaded active   running Fake app
nginx.service          loaded inactive dead    A web server
cron.service           loaded active   running Regular jobs
ssh.service            loaded inactive dead    Secure Shell
systemd-fsck@x.service loaded inactive dead    File System Check
broken.service         loaded failed   failed  Broken one
ghost.service          not-found inactive dead ghost.service
`

const unitFilesOut = `app.service enabled enabled
nginx.service enabled enabled
cron.service static -
ssh.service disabled enabled
`

func sectionOf(t *testing.T, out map[string]any, name string) map[string]any {
	t.Helper()
	s, ok := out[name].(map[string]any)
	if !ok {
		t.Fatalf("no section %s in %v", name, out)
	}
	return s
}

func TestTheSnapshotListsContainersAndTheServicesThatMatter(t *testing.T) {
	f := dockerWithTwoContainers(t)
	cmd := &fakeCommander{out: map[string]string{"systemctl list-units": unitsOut, "systemctl list-unit-files": unitFilesOut}}
	out, err := invoke(t, Options{Docker: f.client(), Commander: cmd, ProcRoot: t.TempDir(), EtcRoot: t.TempDir()}, "inventory.snapshot", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	if out["taken_at"] == "" {
		t.Fatal("no time")
	}
	cs := sectionOf(t, out, "containers")
	if cs["ok"] != true {
		t.Fatalf("%v", cs)
	}
	rows := cs["data"].([]any)
	if len(rows) != 2 {
		t.Fatalf("%v", rows)
	}
	db, web := rows[0].(map[string]any), rows[1].(map[string]any)
	if db["name"] != "db" || db["state"] != "exited" || web["name"] != "web" || web["health"] != "healthy" ||
		web["compose_project"] != "site" || web["compose_service"] != "web" || web["image"] != "nginx:1" {
		t.Fatalf("%v", rows)
	}
	for _, key := range []string{"exit_code", "oom_killed", "status", "id"} {
		if _, has := web[key]; has {
			t.Fatalf("the inventory keeps no %s: %v", key, web)
		}
	}
	svc := sectionOf(t, out, "services")["data"].([]any)
	var names []string
	for _, r := range svc {
		names = append(names, r.(map[string]any)["unit"].(string))
	}
	// active ones, failed ones and enabled ones stay; the idle static unit and the ghost go
	if got := strings.Join(names, ","); got != "app.service,broken.service,cron.service,nginx.service" {
		t.Fatalf("services: %s", got)
	}
	nginx := svc[3].(map[string]any)
	if nginx["active"] != "inactive" || nginx["enabled"] != "enabled" || nginx["description"] != "A web server" {
		t.Fatalf("an enabled service that is down must show: %v", nginx)
	}
}

// the point of the sections: a source that cannot be read is said so, it is not an empty list
func TestASectionThatCannotBeReadSaysSoInsteadOfBeingEmpty(t *testing.T) {
	cmd := &fakeCommander{err: errors.New("exec: \"systemctl\": executable file not found in $PATH")}
	out, err := invoke(t, Options{Commander: cmd, ProcRoot: t.TempDir(), EtcRoot: t.TempDir()}, "inventory.snapshot", `{}`)
	if err != nil {
		t.Fatalf("one section failing must not fail the call: %v", err)
	}
	cs := sectionOf(t, out, "containers")
	if cs["ok"] == true || cs["code"] != "not_configured" || cs["data"] != nil {
		t.Fatalf("no Docker configured: %v", cs)
	}
	ss := sectionOf(t, out, "services")
	if ss["ok"] == true || ss["code"] != "error" || !strings.Contains(ss["error"].(string), "systemctl") {
		t.Fatalf("%v", ss)
	}
	if sectionOf(t, out, "host")["ok"] != true {
		t.Fatal("the host section does not depend on the others")
	}
}

func TestADockerThatFailsIsAnErrorNotAnEmptyHost(t *testing.T) {
	f := newFakeDocker(t)
	f.list = "this is not json"
	out, _ := invoke(t, Options{Docker: f.client(), Commander: &fakeCommander{}}, "inventory.snapshot", `{}`)
	cs := sectionOf(t, out, "containers")
	if cs["ok"] == true || cs["code"] != "error" {
		t.Fatalf("%v", cs)
	}
}

func TestAHostWithoutContainersIsAnEmptyListNotAnError(t *testing.T) {
	f := newFakeDocker(t)
	f.list = "[]"
	out, _ := invoke(t, Options{Docker: f.client(), Commander: &fakeCommander{out: map[string]string{}}}, "inventory.snapshot", `{}`)
	cs := sectionOf(t, out, "containers")
	if cs["ok"] != true {
		t.Fatalf("%v", cs)
	}
	if rows, _ := cs["data"].([]any); len(rows) != 0 {
		t.Fatalf("%v", cs)
	}
}

func TestTheSnapshotTakesNoInputAndStartsOnlyFixedPrograms(t *testing.T) {
	cmd := &fakeCommander{out: map[string]string{}}
	if _, err := invoke(t, Options{Commander: cmd}, "inventory.snapshot", `{"unit":"x"}`); err == nil {
		t.Fatal("the schema takes no input")
	}
	_, _ = invoke(t, Options{Commander: cmd, ProcRoot: t.TempDir(), EtcRoot: t.TempDir()}, "inventory.snapshot", `{}`)
	for _, c := range cmd.calls {
		if c[0] != "systemctl" || (c[1] != "list-units" && c[1] != "list-unit-files") {
			t.Fatalf("started %v", c)
		}
	}
}
