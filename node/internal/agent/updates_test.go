package agent

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/fake"
	"topcoat/node/internal/schematest"
	"topcoat/node/internal/update"
)

var updatePlat = update.Platform{OS: "linux", Arch: "amd64"}

type updateBundle struct {
	manifest []byte
	sig      string
	binary   []byte
	keys     []update.Key
}

func newUpdateBundle(t *testing.T, mutate func(*update.Manifest)) updateBundle {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	keys, err := update.ParseKeys(base64.StdEncoding.EncodeToString(pub))
	if err != nil {
		t.Fatal(err)
	}
	bin := []byte("the new topcoat-node")
	m := update.Manifest{Format: 1, Product: update.Product, Channel: "stable", Version: "0.2.0", OS: "linux", Arch: "amd64",
		Size: int64(len(bin)), SHA256: update.SHA256Hex(bin), Created: "2026-10-07T20:00:00Z"}
	if mutate != nil {
		mutate(&m)
	}
	mb, _ := json.Marshal(m)
	return updateBundle{manifest: mb, sig: update.Sign(priv, mb), binary: bin, keys: keys}
}

type fakeUpdateSource struct {
	b   updateBundle
	err error
}

func (f *fakeUpdateSource) Manifest(context.Context, string) ([]byte, string, error) {
	return f.b.manifest, f.b.sig, f.err
}

func (f *fakeUpdateSource) Binary(_ context.Context, _ string, dst io.Writer, _ int64) error {
	_, err := dst.Write(f.b.binary)
	return err
}

// updateConfig allows node.update on top of proxyConfig.
func updateConfig(extra string) string {
	cfg := strings.Replace(proxyConfig, `"models.proxy"`, `"models.proxy","node.update"`, 1)
	if extra != "" {
		cfg = strings.Replace(cfg, `"capabilities":[]`, extra+`,"capabilities":[]`, 1)
	}
	return cfg
}

func updaterAlive(t *testing.T, dir string, at time.Time) {
	t.Helper()
	updaterAliveAs(t, dir, at, "0.1.0-test")
}

func updaterAliveAs(t *testing.T, dir string, at time.Time, version string) {
	t.Helper()
	l := update.NewLayout(dir)
	if err := l.EnsureDir(); err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(update.Status{AliveAt: at.UTC().Format(time.RFC3339), Version: version, Keys: []string{"0123456789abcdef"}})
	if err := os.WriteFile(filepath.Join(l.Dir, "status.json"), b, 0o644); err != nil { //nolint:gosec // test
		t.Fatal(err)
	}
}

func updateHarness(t *testing.T, b updateBundle, src *fakeUpdateSource, cfg string, mods ...func(*Options)) (*harness, *hubConn, string) {
	t.Helper()
	dir := t.TempDir()
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, dir, fake.DefaultProfile(), append(mods, func(o *Options) {
		o.Update = &UpdateOptions{Keys: b.keys, Plat: updatePlat, Source: func(*Agent) update.Source { return src }}
	})...)
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, cfg)
	return h, hc, dir
}

func TestNodeUpdateStagesAVerifiedBuildForTheUpdater(t *testing.T) {
	b := newUpdateBundle(t, nil)
	dir := t.TempDir()
	updaterAlive(t, dir, time.Now())
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, dir, fake.DefaultProfile(), func(o *Options) {
		o.Update = &UpdateOptions{Keys: b.keys, Plat: updatePlat, Source: func(*Agent) update.Source { return &fakeUpdateSource{b: b} }}
	})
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, updateConfig(""))

	res := hc.action(t, "node.update", `{}`)
	if !res.OK {
		t.Fatalf("node.update: %+v", res.Error)
	}
	hc.waitEvent(t, protocol.EventUpdateStaging)
	d := hc.waitEvent(t, protocol.EventUpdateStaged)
	if d["version"] != "0.2.0" || d["channel"] != "stable" {
		t.Fatalf("event data = %v", d)
	}
	l := update.NewLayout(dir)
	man, sig, bin, err := l.ReadStaged()
	if err != nil || string(man) != string(b.manifest) || sig != b.sig || string(bin) != string(b.binary) {
		t.Fatalf("staged bundle: %v", err)
	}
	if _, err := os.Stat(filepath.Join(l.Dir, "ready")); err != nil {
		t.Fatalf("no ready request: %v", err)
	}
}

func TestNodeUpdateNeedsThePermission(t *testing.T) {
	b := newUpdateBundle(t, nil)
	_, hc, dir := updateHarness(t, b, &fakeUpdateSource{b: b}, proxyConfig) // node.update not allowed
	updaterAlive(t, dir, time.Now())
	if res := hc.action(t, "node.update", `{}`); res.OK || res.Error.Code != protocol.ErrNotAllowed {
		t.Fatalf("%+v", res)
	}
}

func TestNodeUpdateRefusesWithoutAnUpdater(t *testing.T) {
	b := newUpdateBundle(t, nil)
	_, hc, dir := updateHarness(t, b, &fakeUpdateSource{b: b}, updateConfig(""))
	res := hc.action(t, "node.update", `{}`)
	if res.OK || res.Error.Code != protocol.ErrNotImplemented || !strings.Contains(res.Error.Message, "missing") {
		t.Fatalf("%+v", res)
	}
	updaterAlive(t, dir, time.Now().Add(-10*time.Minute))
	res = hc.action(t, "node.update", `{}`)
	if res.OK || !strings.Contains(res.Error.Message, "stale") {
		t.Fatalf("a stale heartbeat: %+v", res)
	}
}

func TestNodeUpdateRefusesABuildWithoutKeys(t *testing.T) {
	b := newUpdateBundle(t, nil)
	b.keys = nil
	_, hc, dir := updateHarness(t, b, &fakeUpdateSource{b: b}, updateConfig(""))
	updaterAlive(t, dir, time.Now())
	res := hc.action(t, "node.update", `{}`)
	if res.OK || res.Error.Code != protocol.ErrNotImplemented || !strings.Contains(res.Error.Message, "no update key") {
		t.Fatalf("%+v", res)
	}
}

func TestNodeUpdateValidatesItsArguments(t *testing.T) {
	b := newUpdateBundle(t, nil)
	_, hc, dir := updateHarness(t, b, &fakeUpdateSource{b: b}, updateConfig(""))
	updaterAlive(t, dir, time.Now())
	for _, args := range []string{`{"version":"banana"}`, `{"version":3}`, `{"channel":"test"}`} {
		if res := hc.action(t, "node.update", args); res.OK || res.Error.Code != protocol.ErrInvalidArgs {
			t.Errorf("%s: %+v", args, res)
		}
	}
}

func TestNodeUpdateReportsAFailedStaging(t *testing.T) {
	b := newUpdateBundle(t, nil)
	src := &fakeUpdateSource{b: b, err: errors.New("HTTP 404 no release")}
	_, hc, dir := updateHarness(t, b, src, updateConfig(""))
	updaterAlive(t, dir, time.Now())
	if res := hc.action(t, "node.update", `{}`); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	d := hc.waitEvent(t, protocol.EventUpdateFailed)
	if r, _ := d["reason"].(string); !strings.Contains(r, "no release") {
		t.Fatalf("event data = %v", d)
	}
	if _, err := os.Stat(filepath.Join(update.NewLayout(dir).Dir, "ready")); err == nil {
		t.Fatal("a failed staging left a ready request")
	}
}

func TestNodeUpdateFollowsTheConfiguredChannel(t *testing.T) {
	b := newUpdateBundle(t, nil) // a "stable" build
	_, hc, dir := updateHarness(t, b, &fakeUpdateSource{b: b}, updateConfig(`"update":{"channel":"test"}`))
	updaterAlive(t, dir, time.Now())
	if res := hc.action(t, "node.update", `{}`); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	d := hc.waitEvent(t, protocol.EventUpdateFailed)
	if r, _ := d["reason"].(string); !strings.Contains(r, `channel "stable"`) {
		t.Fatalf("a build of another channel was staged: %v", d)
	}
}

func TestTheInventoryReportsTheUpdateState(t *testing.T) {
	b := newUpdateBundle(t, nil)
	dir := t.TempDir()
	updaterAlive(t, dir, time.Now())
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, dir, fake.DefaultProfile(), func(o *Options) {
		o.Update = &UpdateOptions{Keys: b.keys, Plat: updatePlat}
	})
	h.start(t)
	hc := hub.accept(t)
	hc.next(t) // hello
	hc.send(t, protocol.TypeConfig, configBody(t, updateConfig(`"update":{"channel":"test"}`)))
	inv := hc.next(t)
	raw, _ := json.Marshal(inv)
	if err := schematest.Validate("inventory", raw); err != nil {
		t.Fatalf("inventory violates the schema: %v", err)
	}
	var body protocol.Inventory
	if err := inv.DecodeBody(&body); err != nil || body.Update == nil {
		t.Fatalf("%v %+v", err, body.Update)
	}
	u := body.Update
	if !u.Capable || u.Updater != protocol.UpdaterOK || u.Channel != "test" || len(u.KeyIDs) != 1 {
		t.Fatalf("update info = %+v", u)
	}
	// the version the running updater wrote into its heartbeat (an old updater shows here)
	if u.UpdaterVersion != "0.1.0-test" {
		t.Fatalf("updater version = %q", u.UpdaterVersion)
	}
}

func TestANodeWithoutUpdateOptionsReportsNothing(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	hc := hub.accept(t)
	hc.next(t)
	hc.send(t, protocol.TypeConfig, configBody(t, testConfig))
	var body protocol.Inventory
	if err := hc.next(t).DecodeBody(&body); err != nil || body.Update != nil {
		t.Fatalf("%v %+v", err, body.Update)
	}
}

func TestTheNodeConfirmsItsVersionOnceTheHubAcceptedIt(t *testing.T) {
	b := newUpdateBundle(t, nil)
	_, _, dir := updateHarness(t, b, &fakeUpdateSource{b: b}, updateConfig(""))
	eventually(t, "the confirmation", func() bool {
		raw, err := os.ReadFile(filepath.Join(update.NewLayout(dir).Dir, "confirmed.json"))
		if err != nil {
			return false
		}
		var c update.Confirmed
		return json.Unmarshal(raw, &c) == nil && c.Version == "0.1.0-test"
	})
}

func TestTheNodeReportsTheResultOfTheLastUpdate(t *testing.T) {
	for _, tc := range []struct {
		name   string
		result update.Result
		kind   string
	}{
		{"applied", update.Result{OK: true, Version: "0.2.0", At: "2026-10-07T20:00:00Z"}, protocol.EventUpdateApplied},
		{"rolled back", update.Result{OK: false, Version: "0.2.0", Reason: "the node did not report back", At: "2026-10-07T20:00:00Z"}, protocol.EventUpdateFailed},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b := newUpdateBundle(t, nil)
			dir := t.TempDir()
			l := update.NewLayout(dir)
			if err := l.EnsureDir(); err != nil {
				t.Fatal(err)
			}
			raw, _ := json.Marshal(tc.result)
			if err := os.WriteFile(filepath.Join(l.Dir, "result.json"), raw, 0o644); err != nil { //nolint:gosec // test
				t.Fatal(err)
			}
			hub := newFakeHub(t)
			h := newHarness(t, hub.srv.URL, dir, fake.DefaultProfile(), func(o *Options) {
				o.Update = &UpdateOptions{Keys: b.keys, Plat: updatePlat}
			})
			h.start(t)
			hc := hub.accept(t)
			// The result is reported before the hub connects, so it arrives replayed.
			if env := hc.next(t); env.Type != protocol.TypeHello {
				t.Fatalf("first message %s, want hello", env.Type)
			}
			hc.send(t, protocol.TypeConfig, configBody(t, updateConfig("")))
			if env := hc.next(t); env.Type != protocol.TypeInventory {
				t.Fatalf("got %s, want inventory", env.Type)
			}
			env := hc.next(t)
			var qe protocol.QueuedEvents
			if env.Type != protocol.TypeQueuedEvents || env.DecodeBody(&qe) != nil || len(qe.Events) != 1 {
				t.Fatalf("got %s %+v, want one queued event", env.Type, qe)
			}
			var ev protocol.Event
			if err := qe.Events[0].DecodeBody(&ev); err != nil || ev.Kind != tc.kind || ev.Data["version"] != "0.2.0" {
				t.Fatalf("event = %+v (%v), want %s", ev, err, tc.kind)
			}
			if l.TakeResult() != nil {
				t.Fatal("the result would be reported again")
			}
		})
	}
}

func TestTheNodeReportsAResultThatAppearsAfterItStarted(t *testing.T) {
	b := newUpdateBundle(t, nil)
	dir := t.TempDir()
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, dir, fake.DefaultProfile(), func(o *Options) {
		o.Update = &UpdateOptions{Keys: b.keys, Plat: updatePlat, ResultPoll: 20 * time.Millisecond}
	})
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, updateConfig(""))
	// The updater writes its result only once the new node is up and confirmed.
	l := update.NewLayout(dir)
	raw, _ := json.Marshal(update.Result{OK: true, Version: "0.2.0", At: "2026-10-07T20:00:00Z"})
	if err := os.WriteFile(filepath.Join(l.Dir, "result.json"), raw, 0o644); err != nil { //nolint:gosec // test
		t.Fatal(err)
	}
	if d := hc.waitEvent(t, protocol.EventUpdateApplied); d["version"] != "0.2.0" {
		t.Fatalf("event data = %v", d)
	}
}

func TestANewUpdaterVersionIsReportedWithoutAReconnect(t *testing.T) {
	// live 2026-11-09: the new node connected six seconds before the updater restarted with the
	// new program, and the hub showed the old updater version until the next reconnect
	b := newUpdateBundle(t, nil)
	dir := t.TempDir()
	updaterAliveAs(t, dir, time.Now(), "0.2.5")
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, dir, fake.DefaultProfile(), func(o *Options) {
		o.Update = &UpdateOptions{Keys: b.keys, Plat: updatePlat, ResultPoll: 20 * time.Millisecond}
	})
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, updateConfig(""))
	updaterAliveAs(t, dir, time.Now(), "0.2.6")
	inv := hc.waitType(t, protocol.TypeInventory)
	var body protocol.Inventory
	if err := inv.DecodeBody(&body); err != nil || body.Update == nil {
		t.Fatalf("%v %+v", err, body.Update)
	}
	if body.Update.UpdaterVersion != "0.2.6" {
		t.Fatalf("updater version = %q, want the new one", body.Update.UpdaterVersion)
	}
}

func TestAnUpdaterThatStoppedIsReportedWithoutAReconnect(t *testing.T) {
	// live 2026-11-09: on Windows the updater stayed stopped after an update; the hub kept "ok"
	b := newUpdateBundle(t, nil)
	dir := t.TempDir()
	updaterAliveAs(t, dir, time.Now(), "0.2.7")
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, dir, fake.DefaultProfile(), func(o *Options) {
		o.Update = &UpdateOptions{Keys: b.keys, Plat: updatePlat, ResultPoll: 20 * time.Millisecond}
	})
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, updateConfig(""))
	updaterAliveAs(t, dir, time.Now().Add(-10*time.Minute), "0.2.7") // the heartbeat stops
	inv := hc.waitType(t, protocol.TypeInventory)
	var body protocol.Inventory
	if err := inv.DecodeBody(&body); err != nil || body.Update == nil {
		t.Fatalf("%v %+v", err, body.Update)
	}
	if body.Update.Updater != protocol.UpdaterStale {
		t.Fatalf("updater = %q, want stale", body.Update.Updater)
	}
}

// --- HubSource ----------------------------------------------------------------

func TestHubSourceFetchesWithASignedNonce(t *testing.T) {
	b := newUpdateBundle(t, nil)
	hub := newFakeHub(t)
	var seenQuery []string
	hub.update = func(w http.ResponseWriter, r *http.Request) {
		seenQuery = append(seenQuery, r.URL.Path+"?"+r.URL.RawQuery)
		switch r.URL.Path {
		case "/api/node/update/manifest":
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]string{"manifest": string(b.manifest), "signature": b.sig})
		case "/api/node/update/binary":
			_, _ = w.Write(b.binary)
		default:
			http.NotFound(w, r)
		}
	}
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	src := &HubSource{Agent: h.agent}
	ctx := context.Background()
	man, sig, err := src.Manifest(ctx, "0.2.0")
	if err != nil || string(man) != string(b.manifest) || sig != b.sig {
		t.Fatalf("manifest: %v", err)
	}
	var sb strings.Builder
	if err := src.Binary(ctx, "0.2.0", &sb, 1<<20); err != nil || sb.String() != string(b.binary) {
		t.Fatalf("binary: %v", err)
	}
	if len(seenQuery) != 2 || seenQuery[0] != "/api/node/update/manifest?version=0.2.0" {
		t.Fatalf("requests = %v", seenQuery)
	}
	// "newest" sends no version at all
	if _, _, err := src.Manifest(ctx, ""); err != nil || seenQuery[2] != "/api/node/update/manifest?" {
		t.Fatalf("newest: %v %v", err, seenQuery)
	}
	// too large for the limit
	var sink strings.Builder
	if err := src.Binary(ctx, "0.2.0", &sink, int64(len(b.binary))); err == nil {
		t.Fatal("a program of exactly the limit (so possibly longer) was accepted")
	}
}

func TestHubSourceReportsTheHubsAnswer(t *testing.T) {
	hub := newFakeHub(t)
	hub.update = func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `{"detail":{"code":"no_release"}}`, http.StatusNotFound)
	}
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	_, _, err := (&HubSource{Agent: h.agent}).Manifest(context.Background(), "")
	if err == nil || !strings.Contains(err.Error(), "404") || !strings.Contains(err.Error(), "no_release") {
		t.Fatalf("err = %v", err)
	}
}

func TestEndpointURL(t *testing.T) {
	got, err := EndpointURL("https://hub.example.lan:8443/", "manifest", versionQuery("0.2.0"))
	if err != nil || got != "https://hub.example.lan:8443/api/node/update/manifest?version=0.2.0" {
		t.Fatalf("%q %v", got, err)
	}
	if _, err := EndpointURL("ftp://hub", "manifest", nil); err == nil {
		t.Fatal("ftp accepted")
	}
}
