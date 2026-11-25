package tools

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"topcoat/node/internal/providers/capability"
)

// A project "shop" with an app that depends on the db and a cache, a stopped worker, and an
// unrelated container.
const shopList = `[
 {"Id":"1111111111110000","Names":["/shop-app-1"],"Image":"shop:1","State":"running","Status":"Up","Labels":{"com.docker.compose.project":"shop","com.docker.compose.service":"app","com.docker.compose.depends_on":"db:service_healthy:true,cache:service_started:false"}},
 {"Id":"2222222222220000","Names":["/shop-db-1"],"Image":"postgres:16","State":"running","Status":"Up","Labels":{"com.docker.compose.project":"shop","com.docker.compose.service":"db"}},
 {"Id":"3333333333330000","Names":["/shop-cache-1"],"Image":"redis:7","State":"running","Status":"Up","Labels":{"com.docker.compose.project":"shop","com.docker.compose.service":"cache","com.docker.compose.depends_on":"db:service_started:false"}},
 {"Id":"4444444444440000","Names":["/shop-worker-1"],"Image":"shop:1","State":"exited","Status":"Exited (0)","Labels":{"com.docker.compose.project":"shop","com.docker.compose.service":"worker"}},
 {"Id":"5555555555550000","Names":["/other"],"Image":"x","State":"running","Status":"Up","Labels":{}}
]`

func inspectJSON(name, state string) string {
	return `{"Id":"x","Name":"/` + name + `","Config":{"Image":"i","Tty":false},"State":{"Status":"` + state + `","Running":` + map[bool]string{true: "true", false: "false"}[state == "running"] + `},"RestartCount":1}`
}

func shopDocker(t *testing.T) *fakeDocker {
	f := newFakeDocker(t)
	f.list = shopList
	for _, c := range []struct{ id, name, state string }{
		{"1111111111110000", "shop-app-1", "running"}, {"2222222222220000", "shop-db-1", "running"},
		{"3333333333330000", "shop-cache-1", "running"}, {"4444444444440000", "shop-worker-1", "exited"},
	} {
		f.inspect[c.id] = inspectJSON(c.name, c.state)
		f.inspect[c.name] = f.inspect[c.id]
	}
	f.logs["1111111111110000"] = frames([2]string{"out", "2026-10-02T10:00:03.000000000Z GET /health 200\n"}, [2]string{"err", "2026-10-02T10:00:05.000000000Z ERROR cannot reach db\n"})
	f.logs["2222222222220000"] = frames([2]string{"out", "2026-10-02T10:00:01.000000000Z database system is ready\n"}, [2]string{"out", "2026-10-02T10:00:04.000000000Z checkpoint complete\n"})
	f.logs["3333333333330000"] = frames([2]string{"out", "2026-10-02T10:00:02.000000000Z Ready to accept connections password=hunter2hunter2\n"})
	f.logs["4444444444440000"] = frames([2]string{"out", "2026-10-02T09:00:00.000000000Z worker done\n"})
	return f
}

// restartRecorder is a write proxy that records the order of restarts and fails named ones.
type restartRecorder struct {
	mu    sync.Mutex
	names []string
	fail  map[string]bool
	srv   *httptest.Server
}

func newRestartRecorder(t *testing.T, fail ...string) *restartRecorder {
	r := &restartRecorder{fail: map[string]bool{}}
	for _, f := range fail {
		r.fail[f] = true
	}
	r.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		name := strings.TrimSuffix(strings.TrimPrefix(req.URL.Path, "/containers/"), "/restart")
		r.mu.Lock()
		r.names = append(r.names, name)
		r.mu.Unlock()
		if req.Method != http.MethodPost || r.fail[name] {
			http.Error(w, "refused", http.StatusForbidden)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(r.srv.Close)
	return r
}

func (r *restartRecorder) writer(t *testing.T) *DockerWriter {
	w, err := NewDockerWriter("tcp://" + strings.TrimPrefix(r.srv.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	return w
}

func TestComposeRestartIsAWriteCapabilityOfTheSameGateAsDockerRestart(t *testing.T) {
	p := newRestartRecorder(t)
	r := NewRegistry(Options{DockerWrite: p.writer(t), RestartContainers: []string{"shop-*"}})
	c, ok := r.Get("compose.restart")
	if !ok {
		t.Fatal("compose.restart missing")
	}
	if capability.InfoOf(c).ReadOnly {
		t.Fatal("a restart must not be read-only")
	}
	for _, o := range []Options{{}, {RestartContainers: []string{"shop-*"}}, {DockerWrite: p.writer(t)}} {
		if _, ok := NewRegistry(o).Get("compose.restart"); ok {
			t.Fatalf("compose.restart offered with %+v", o)
		}
	}
}

func TestComposeRestartRestartsTheRunningContainersDependenciesFirstAndLeavesStoppedOnesAlone(t *testing.T) {
	d, p := shopDocker(t), newRestartRecorder(t)
	o := Options{Docker: d.client(), DockerWrite: p.writer(t), RestartContainers: []string{"shop-*"}}
	out, err := invoke(t, o, "compose.restart", `{"project":"shop"}`)
	if err != nil {
		t.Fatal(err)
	}
	// db first, then cache (depends on db), then app (depends on both); the stopped worker is not started
	if got := strings.Join(p.names, ","); got != "shop-db-1,shop-cache-1,shop-app-1" {
		t.Fatalf("restart order %s", got)
	}
	if out["restarted"] != true || out["project"] != "shop" {
		t.Fatalf("%v", out)
	}
	cs := out["containers"].([]any)
	if len(cs) != 3 || cs[0].(map[string]any)["container"] != "shop-db-1" || cs[0].(map[string]any)["state"] != "running" {
		t.Fatalf("%v", cs)
	}
	left := out["left_stopped"].([]any)
	if len(left) != 1 || !strings.Contains(left[0].(string), "shop-worker-1") {
		t.Fatalf("%v", out)
	}
}

func TestComposeRestartRefusesTheWholeStackWhenOneRunningContainerIsOffTheList(t *testing.T) {
	d, p := shopDocker(t), newRestartRecorder(t)
	o := Options{Docker: d.client(), DockerWrite: p.writer(t), RestartContainers: []string{"shop-app-*", "shop-cache-*"}}
	_, err := invoke(t, o, "compose.restart", `{"project":"shop"}`)
	if err == nil || !strings.Contains(err.Error(), "shop-db-1") || !strings.Contains(err.Error(), "nothing was restarted") {
		t.Fatalf("%v", err)
	}
	if len(p.names) != 0 {
		t.Fatalf("the proxy was asked: %v", p.names)
	}
}

func TestComposeRestartStopsAtTheFirstFailureAndSaysWhatWasDone(t *testing.T) {
	d, p := shopDocker(t), newRestartRecorder(t, "shop-cache-1")
	o := Options{Docker: d.client(), DockerWrite: p.writer(t), RestartContainers: []string{"shop-*"}}
	_, err := invoke(t, o, "compose.restart", `{"project":"shop"}`)
	if err == nil || !strings.Contains(err.Error(), "restarted shop-db-1; then restarting shop-cache-1 failed") || !strings.Contains(err.Error(), "not touched") {
		t.Fatalf("%v", err)
	}
	if got := strings.Join(p.names, ","); got != "shop-db-1,shop-cache-1" { // app was never asked
		t.Fatalf("%s", got)
	}
}

func TestComposeRestartRefusesWhatIsNotAProjectOrIsNotRunningOrIsBadInput(t *testing.T) {
	d, p := shopDocker(t), newRestartRecorder(t)
	o := Options{Docker: d.client(), DockerWrite: p.writer(t), RestartContainers: []string{"*"}}
	if _, err := invoke(t, o, "compose.restart", `{"project":"nope"}`); err == nil || !strings.Contains(err.Error(), "no container of compose project") {
		t.Fatalf("%v", err)
	}
	for _, bad := range []string{`{"project":"../shop"}`, `{"project":"shop","all":true}`, `{"project":"shop","timeout_seconds":99}`, `{}`} {
		if _, err := invoke(t, o, "compose.restart", bad); err == nil {
			t.Fatalf("accepted %s", bad)
		}
	}
	// a project whose containers are all stopped is not started by a "restart"
	d.list = `[{"Id":"4444444444440000","Names":["/shop-worker-1"],"Image":"x","State":"exited","Status":"Exited","Labels":{"com.docker.compose.project":"shop","com.docker.compose.service":"worker"}}]`
	if _, err := invoke(t, o, "compose.restart", `{"project":"shop"}`); err == nil || !strings.Contains(err.Error(), "does not start") {
		t.Fatalf("%v", err)
	}
	if len(p.names) != 0 {
		t.Fatalf("a refused call reached the proxy: %v", p.names)
	}
}

func TestDependencyOrderIsStableAndSurvivesACycle(t *testing.T) {
	mk := func(name, svc, dep string) apiContainerSummary {
		return apiContainerSummary{Names: []string{"/" + name}, Labels: map[string]string{labelComposeService: svc, labelComposeDependsOn: dep}}
	}
	order := func(cs ...apiContainerSummary) string {
		var names []string
		for _, c := range dependencyOrder(cs) {
			names = append(names, trimName(c.Names))
		}
		return strings.Join(names, ",")
	}
	// sorted by name on the way in: a < b < c; c is the dependency of a
	if got := order(mk("a", "a", "c:service_started:false"), mk("b", "b", ""), mk("c", "c", "")); got != "b,c,a" {
		t.Fatalf("%s", got)
	}
	// a cycle does not hang and does not drop a container
	if got := order(mk("a", "a", "b:x:y"), mk("b", "b", "a:x:y")); got != "a,b" {
		t.Fatalf("%s", got)
	}
}

func TestComposeLogsMergesTheContainersByTimeMarksTheServiceAndMasksSecrets(t *testing.T) {
	d := shopDocker(t)
	out, err := invoke(t, Options{Docker: d.client()}, "compose.logs", `{"project":"shop"}`)
	if err != nil {
		t.Fatal(err)
	}
	log := out["log"].(string)
	want := []string{"[worker] worker done", "[db] database system is ready", "[cache] Ready to accept", "[app] GET /health", "[db] checkpoint", "[app] ERROR cannot reach db"}
	last := -1
	for _, w := range want {
		i := strings.Index(log, w)
		if i < 0 || i < last {
			t.Fatalf("%q missing or out of order in:\n%s", w, log)
		}
		last = i
	}
	if strings.Contains(log, "hunter2hunter2") {
		t.Fatalf("a secret is in the log: %s", log)
	}
	if out["containers"] != float64(4) {
		t.Fatalf("%v", out)
	}
	// the other container is not part of the project
	if strings.Contains(log, "other") {
		t.Fatal("a container outside the project")
	}
}

func TestComposeLogsFiltersByServiceAndTextAndSaysWhenTheProjectIsUnknown(t *testing.T) {
	d := shopDocker(t)
	out, err := invoke(t, Options{Docker: d.client()}, "compose.logs", `{"project":"shop","contains":"ERROR"}`)
	if err != nil {
		t.Fatal(err)
	}
	if log := out["log"].(string); !strings.Contains(log, "cannot reach db") || strings.Contains(log, "checkpoint") || out["lines"] != float64(1) {
		t.Fatalf("%v", out)
	}
	out, err = invoke(t, Options{Docker: d.client()}, "compose.logs", `{"project":"shop","service":"db","lines":5}`)
	if err != nil {
		t.Fatal(err)
	}
	if log := out["log"].(string); strings.Contains(log, "[app]") || !strings.Contains(log, "[db]") || out["containers"] != float64(1) {
		t.Fatalf("%v", out)
	}
	if _, err := invoke(t, Options{Docker: d.client()}, "compose.logs", `{"project":"nope"}`); err == nil || !strings.Contains(err.Error(), "no container of compose project") {
		t.Fatalf("%v", err)
	}
	for _, bad := range []string{`{"project":"shop","lines":999}`, `{"project":"shop","since":"yesterday","until":"2020-01-01"}`, `{"project":"a b"}`} {
		if _, err := invoke(t, Options{Docker: d.client()}, "compose.logs", bad); err == nil {
			t.Fatalf("accepted %s", bad)
		}
	}
}

func TestComposeLogsReportsAContainerItCouldNotReadInsteadOfFailingTheStack(t *testing.T) {
	d := shopDocker(t)
	delete(d.logs, "3333333333330000") // the fake answers 404 for it
	out, err := invoke(t, Options{Docker: d.client()}, "compose.logs", `{"project":"shop"}`)
	if err != nil {
		t.Fatal(err)
	}
	un := out["unreadable"].([]any)
	if len(un) != 1 || un[0].(map[string]any)["container"] != "shop-cache-1" || !strings.Contains(out["log"].(string), "[db]") {
		t.Fatalf("%v", out)
	}
}
