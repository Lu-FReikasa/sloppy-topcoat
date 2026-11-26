package agent

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"

	"topcoat/node/internal/backoff"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/fake"
	"topcoat/node/internal/providers/runner"
	"topcoat/node/internal/schematest"
	"topcoat/node/internal/store"
)

const (
	testNodeID  = "3f0c2a4e-8a51-4b7e-9a0e-1f2d3c4b5a60"
	waitTimeout = 5 * time.Second
)

// testKey is the node key the fake hub knows the public half of.
var testKey = ed25519.NewKeyFromSeed(bytes.Repeat([]byte{7}, ed25519.SeedSize))

// testConfig is a config as the hub would send it.
const testConfig = `{"roles":["gpu"],"runner":{"type":"fake"},` +
	`"allowed_actions":["models.list","models.load","models.unload","models.download"],` +
	`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,` +
	`"watcher":{"grace_seconds":0,"cooldown_seconds":0,"rules":[{"process":"game.exe"}]},"capabilities":[]}`

// --- fake hub ----------------------------------------------------------------

type fakeHub struct {
	t      *testing.T
	srv    *httptest.Server
	conns  chan *hubConn
	reject atomic.Bool
	dials  atomic.Int32

	mu     sync.Mutex
	nonces map[string]bool // issued, not yet used
	seq    int
	// update serves /api/node/update/* (E2-06) for the requests that carry a valid signature.
	update http.HandlerFunc
	// builds serves /api/node/builds/* (NS-01) for the requests that carry a valid signature.
	builds http.HandlerFunc
}

type hubConn struct {
	t           *testing.T
	c           *websocket.Conn
	msgs        chan protocol.Envelope
	closeStatus atomic.Int32
	closed      chan struct{}
	// stash holds what action() read while waiting for its result: a model load reports its
	// event and heartbeat before the result, and later waits must still find them.
	stash []protocol.Envelope
}

func newFakeHub(t *testing.T) *fakeHub {
	t.Helper()
	h := &fakeHub{t: t, conns: make(chan *hubConn, 16), nonces: map[string]bool{}}
	h.srv = httptest.NewServer(http.HandlerFunc(h.serve))
	t.Cleanup(h.srv.Close)
	return h
}

func (h *fakeHub) serve(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/api/node/challenge" {
		h.serveChallenge(w, r)
		return
	}
	if strings.HasPrefix(r.URL.Path, "/api/node/update/") && h.update != nil {
		if !h.authorized(r.Header.Get("Authorization")) {
			http.Error(w, `{"detail":"invalid signature"}`, http.StatusForbidden)
			return
		}
		h.update(w, r)
		return
	}
	if strings.HasPrefix(r.URL.Path, "/api/node/builds/") && h.builds != nil {
		if !h.authorized(r.Header.Get("Authorization")) {
			http.Error(w, `{"detail":"invalid signature"}`, http.StatusForbidden)
			return
		}
		h.builds(w, r)
		return
	}
	h.dials.Add(1)
	if r.URL.Path != "/api/node/ws" {
		http.NotFound(w, r)
		return
	}
	if h.reject.Load() || !h.authorized(r.Header.Get("Authorization")) {
		http.Error(w, `{"detail":"invalid signature"}`, http.StatusForbidden)
		return
	}
	c, err := websocket.Accept(w, r, nil)
	if err != nil {
		h.t.Errorf("accept: %v", err)
		return
	}
	hc := &hubConn{t: h.t, c: c, msgs: make(chan protocol.Envelope, 256), closed: make(chan struct{})}
	hc.closeStatus.Store(-1)
	go hc.readLoop()
	h.conns <- hc
}

func (h *fakeHub) serveChallenge(w http.ResponseWriter, r *http.Request) {
	var req protocol.ChallengeRequest
	raw, _ := io.ReadAll(r.Body)
	if err := schematest.Validate("challenge_request", raw); err != nil {
		h.t.Errorf("challenge request violates the schema: %v", err)
	}
	if err := json.Unmarshal(raw, &req); err != nil || req.NodeID != testNodeID {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	h.mu.Lock()
	h.seq++
	nonce := fmt.Sprintf("nonce-%04d-abcdefghijklmnopqrstuvwxyz0123", h.seq)
	h.nonces[nonce] = true
	h.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(protocol.ChallengeResponse{Nonce: nonce, ExpiresIn: 60})
}

// authorized checks the signed, single-use nonce like the hub does.
func (h *fakeHub) authorized(header string) bool {
	scheme, value, _ := strings.Cut(header, " ")
	parts := strings.Split(value, ".")
	if scheme != protocol.AuthScheme || len(parts) != 3 || parts[0] != testNodeID {
		return false
	}
	h.mu.Lock()
	fresh := h.nonces[parts[1]]
	delete(h.nonces, parts[1])
	h.mu.Unlock()
	sig, err := base64.RawURLEncoding.DecodeString(parts[2])
	return fresh && err == nil &&
		ed25519.Verify(testKey.Public().(ed25519.PublicKey), protocol.AuthMessage(testNodeID, parts[1]), sig)
}

func (hc *hubConn) readLoop() {
	defer close(hc.closed)
	for {
		_, data, err := hc.c.Read(context.Background())
		if err != nil {
			hc.closeStatus.Store(int32(websocket.CloseStatus(err)))
			return
		}
		var env protocol.Envelope
		if err := json.Unmarshal(data, &env); err != nil {
			hc.t.Errorf("node sent invalid JSON: %v", err)
			continue
		}
		// Every node -> hub message must match its schema.
		if err := schematest.Validate(env.Type, data); err != nil {
			hc.t.Errorf("node sent %s that violates the schema: %v\n%s", env.Type, err, data)
		}
		hc.msgs <- env
	}
}

func (h *fakeHub) accept(t *testing.T) *hubConn {
	t.Helper()
	select {
	case hc := <-h.conns:
		return hc
	case <-time.After(waitTimeout):
		t.Fatal("timeout waiting for the node to connect")
	}
	return nil
}

func (hc *hubConn) next(t *testing.T) protocol.Envelope {
	t.Helper()
	select {
	case env := <-hc.msgs:
		return env
	case <-time.After(waitTimeout):
		t.Fatal("timeout waiting for a message from the node")
	}
	return protocol.Envelope{}
}

// waitFor returns the first message matching pred, skipping others.
func (hc *hubConn) waitFor(t *testing.T, what string, pred func(protocol.Envelope) bool) protocol.Envelope {
	t.Helper()
	return hc.wait(t, what, pred, false)
}

// wait is waitFor; with keep the events and heartbeats it skips are stashed for later waits.
func (hc *hubConn) wait(t *testing.T, what string, pred func(protocol.Envelope) bool, keep bool) protocol.Envelope {
	t.Helper()
	for i, env := range hc.stash {
		if pred(env) {
			hc.stash = append(hc.stash[:i], hc.stash[i+1:]...)
			return env
		}
	}
	deadline := time.After(waitTimeout)
	for {
		select {
		case env := <-hc.msgs:
			if pred(env) {
				return env
			}
			if keep {
				hc.stash = append(hc.stash, env)
			}
		case <-deadline:
			t.Fatalf("timeout waiting for %s", what)
		}
	}
}

func (hc *hubConn) waitType(t *testing.T, typ string) protocol.Envelope {
	t.Helper()
	return hc.waitFor(t, typ, func(e protocol.Envelope) bool { return e.Type == typ })
}

func (hc *hubConn) waitEvent(t *testing.T, kind string) map[string]any {
	t.Helper()
	var ev protocol.Event
	hc.waitFor(t, "event "+kind, func(e protocol.Envelope) bool {
		if e.Type != protocol.TypeEvent {
			return false
		}
		ev = protocol.Event{}
		_ = e.DecodeBody(&ev)
		return ev.Kind == kind
	})
	return ev.Data
}

func (hc *hubConn) send(t *testing.T, typ string, body any) string {
	t.Helper()
	env, err := protocol.New(typ, body, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(env)
	if err := schematest.Validate(typ, b); err != nil {
		t.Fatalf("test hub about to send an invalid %s: %v", typ, err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), waitTimeout)
	defer cancel()
	if err := hc.c.Write(ctx, websocket.MessageText, b); err != nil {
		t.Fatalf("hub write: %v", err)
	}
	return env.ID
}

func configBody(t *testing.T, cfg string) protocol.ConfigBody {
	t.Helper()
	h, err := protocol.ConfigHash([]byte(cfg))
	if err != nil {
		t.Fatal(err)
	}
	return protocol.ConfigBody{ConfigHash: h, Config: json.RawMessage(cfg)}
}

// action sends an action and waits for its action_result.
func (hc *hubConn) action(t *testing.T, name string, args string) protocol.ActionResult {
	t.Helper()
	id := hc.send(t, protocol.TypeAction, map[string]any{"name": name, "args": json.RawMessage(args)})
	env := hc.wait(t, "action_result for "+name, func(e protocol.Envelope) bool {
		return e.Type == protocol.TypeActionResult && e.Re == id
	}, true)
	var res protocol.ActionResult
	if err := env.DecodeBody(&res); err != nil {
		t.Fatal(err)
	}
	return res
}

// handshake runs hello -> config -> inventory -> (queued_events) -> heartbeat.
func (hc *hubConn) handshake(t *testing.T, cfg string) {
	t.Helper()
	if env := hc.next(t); env.Type != protocol.TypeHello {
		t.Fatalf("first message %s, want hello", env.Type)
	}
	hc.send(t, protocol.TypeConfig, configBody(t, cfg))
	if env := hc.next(t); env.Type != protocol.TypeInventory {
		t.Fatalf("got %s, want inventory", env.Type)
	}
	hc.waitType(t, protocol.TypeHeartbeat)
}

// --- agent harness -------------------------------------------------------------

type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

type harness struct {
	agent   *Agent
	world   *fake.World
	watcher *fake.Watcher
	dir     *store.Dir
	logs    *syncBuffer
	sleeps  chan time.Duration
	cancel  context.CancelFunc
	done    chan struct{}
}

func newHarness(t *testing.T, hubURL, dirPath string, profile fake.Profile, mods ...func(*Options)) *harness {
	t.Helper()
	d, err := store.Open(dirPath)
	if err != nil {
		t.Fatal(err)
	}
	world, err := fake.NewWorld(profile)
	if err != nil {
		t.Fatal(err)
	}
	h := &harness{world: world, watcher: fake.NewWatcher(), dir: d, logs: &syncBuffer{}, sleeps: make(chan time.Duration, 1024)}
	var out io.Writer = h.logs
	fr := world.Runner()
	opts := Options{
		Identity:          store.Identity{HubURL: hubURL, NodeID: testNodeID, InsecureHTTP: true},
		Key:               testKey,
		Dir:               d,
		GPU:               world.GPU(),
		Activity:          world.Activity(),
		Network:           world.Network(),
		Sleeper:           world.Sleeper(),
		SysInfo:           world.SysInfo(),
		Watcher:           h.watcher,
		NewRunner:         func(protocol.RunnerConfig) runner.ModelRunner { return fr },
		AgentVersion:      "0.1.0-test",
		Logger:            slog.New(slog.NewTextHandler(out, &slog.HandlerOptions{Level: slog.LevelDebug})),
		Backoff:           &backoff.Backoff{Min: 10 * time.Millisecond, Max: 80 * time.Millisecond},
		HandshakeTimeout:  3 * time.Second,
		HeartbeatInterval: time.Hour,
		IdleCheckInterval: time.Hour,
	}
	opts.Sleep = func(ctx context.Context, d time.Duration) error {
		select {
		case h.sleeps <- d:
		default:
		}
		return sleepCtx(ctx, d)
	}
	for _, m := range mods {
		m(&opts)
	}
	a, err := New(opts)
	if err != nil {
		t.Fatal(err)
	}
	h.agent = a
	// A failed test shows the end of the node's log: a timeout on the hub side alone does not
	// say where the node was (#72).
	t.Cleanup(func() {
		if t.Failed() {
			l := h.logs.String()
			if len(l) > 8<<10 {
				l = "...\n" + l[len(l)-8<<10:]
			}
			t.Logf("node log:\n%s", l)
		}
	})
	return h
}

func (h *harness) start(t *testing.T) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	h.cancel = cancel
	h.done = make(chan struct{})
	go func() {
		defer close(h.done)
		if err := h.agent.Run(ctx); err != nil {
			t.Errorf("Run: %v", err)
		}
	}()
	t.Cleanup(h.stop)
}

func (h *harness) stop() {
	if h.cancel == nil {
		return
	}
	h.cancel()
	<-h.done
	h.cancel = nil
}

func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(waitTimeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timeout waiting for %s", what)
}

func loadedNames(t *testing.T, w *fake.World) []string {
	t.Helper()
	l, err := w.Runner().Loaded(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, m := range l {
		out = append(out, m.Name)
	}
	return out
}

func downURL(t *testing.T) string {
	t.Helper()
	srv := httptest.NewServer(http.NotFoundHandler())
	u := srv.URL
	srv.Close()
	return u
}

// --- tests ---------------------------------------------------------------------

func TestHandshakeOrderAndGracefulClose(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	hc := hub.accept(t)

	env := hc.next(t)
	if env.Type != protocol.TypeHello {
		t.Fatalf("first message %s", env.Type)
	}
	var hello protocol.Hello
	_ = env.DecodeBody(&hello)
	if hello.NodeID != testNodeID || hello.ConfigHash != nil || hello.ProtocolVersion != 1 || hello.AgentVersion != "0.1.0-test" {
		t.Fatalf("hello %+v", hello)
	}
	body := configBody(t, testConfig)
	hc.send(t, protocol.TypeConfig, body)

	env = hc.next(t)
	if env.Type != protocol.TypeInventory {
		t.Fatalf("second message %s, want inventory", env.Type)
	}
	var inv protocol.Inventory
	_ = env.DecodeBody(&inv)
	if len(inv.Hardware.GPUs) != 1 || inv.Hardware.GPUs[0].VRAMTotalMB != 16384 || len(inv.Models) != 3 || inv.Runner.Type != "fake" || !inv.Runner.Available {
		t.Fatalf("inventory %+v", inv)
	}
	env = hc.next(t)
	if env.Type != protocol.TypeHeartbeat {
		t.Fatalf("third message %s, want heartbeat (queue was empty)", env.Type)
	}
	var hb protocol.Heartbeat
	_ = env.DecodeBody(&hb)
	if hb.State != protocol.StateAvailable || hb.RAMUsedMB != 9000 || len(hb.GPUs) != 1 || hb.GPUs[0].VRAMUsedMB != 800 {
		t.Fatalf("heartbeat %+v", hb)
	}

	cc, ok, err := h.dir.LoadConfig()
	if err != nil || !ok || cc.ConfigHash != body.ConfigHash {
		t.Fatalf("config not cached: %+v %v %v", cc, ok, err)
	}

	h.stop()
	select {
	case <-hc.closed:
	case <-time.After(waitTimeout):
		t.Fatal("connection not closed on shutdown")
	}
	if st := websocket.StatusCode(hc.closeStatus.Load()); st != websocket.StatusNormalClosure {
		t.Fatalf("close status %v, want normal closure", st)
	}
}

func TestPeriodicHeartbeat(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), func(o *Options) { o.HeartbeatInterval = 20 * time.Millisecond })
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, testConfig)
	for i := 0; i < 3; i++ {
		hc.waitType(t, protocol.TypeHeartbeat)
	}
}

// The heartbeat carries the CPU utilization when the system can measure it and leaves the field
// out when it cannot (an older node sends none either).
func TestHeartbeatCarriesCPUUtilisationWhenKnown(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), func(o *Options) { o.HeartbeatInterval = 20 * time.Millisecond })
	h.world.SetCPU(37.5)
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, testConfig)
	var body protocol.Heartbeat
	hb := hc.waitType(t, protocol.TypeHeartbeat)
	if err := hb.DecodeBody(&body); err != nil {
		t.Fatal(err)
	}
	if body.CPUUtil == nil || *body.CPUUtil != 37.5 {
		t.Fatalf("cpu_util = %v, want 37.5", body.CPUUtil)
	}
	h.world.SetCPU(0)
	for range 20 {
		hb = hc.waitType(t, protocol.TypeHeartbeat)
		body = protocol.Heartbeat{}
		_ = hb.DecodeBody(&body)
		if body.CPUUtil == nil {
			return
		}
	}
	t.Fatal("cpu_util stayed in the heartbeat although the system cannot measure it")
}

// A load without context_length uses the configured one (per model, else node default), an
// explicit one wins.
func TestLoadUsesConfiguredContext(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	hc := hub.accept(t)
	cfg := strings.Replace(testConfig, `"runner":{"type":"fake"}`,
		`"runner":{"type":"fake","default_context_length":16384,"model_context_lengths":{"fake-llm-8b-q4":32768}}`, 1)
	hc.handshake(t, cfg)
	// Heartbeats queued before the last load or unload may still list the previous instance of
	// the model, so read on until the context is the wanted one (the last value is reported).
	loadedContext := func(model string, want int) int {
		t.Helper()
		got := -1
		for range 20 {
			hb := hc.waitType(t, protocol.TypeHeartbeat)
			var body protocol.Heartbeat
			_ = hb.DecodeBody(&body)
			for _, l := range body.Loaded {
				if l.Name == model {
					if got = l.ContextLength; got == want {
						return got
					}
				}
			}
		}
		return got
	}
	if res := hc.action(t, "models.load", `{"model":"fake-llm-8b-q4"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	if got := loadedContext("fake-llm-8b-q4", 32768); got != 32768 {
		t.Errorf("per-model context = %d, want 32768", got)
	}
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	if got := loadedContext("fake-llm-3b-q8", 16384); got != 16384 {
		t.Errorf("node default context = %d, want 16384", got)
	}
	if res := hc.action(t, "models.unload", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("unload: %+v", res.Error)
	}
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8","context_length":2048}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	if got := loadedContext("fake-llm-3b-q8", 2048); got != 2048 {
		t.Errorf("explicit context = %d, want 2048", got)
	}
}

func TestLoadUsesConfiguredSlots(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	hc := hub.accept(t)
	cfg := strings.Replace(testConfig, `"runner":{"type":"fake"}`,
		`"runner":{"type":"fake","n_parallel":2,"model_n_parallel":{"fake-llm-8b-q4":1}}`, 1)
	hc.handshake(t, cfg)
	slots := func(model string, want int) int {
		t.Helper()
		got := -1
		for range 20 {
			hb := hc.waitType(t, protocol.TypeHeartbeat)
			var body protocol.Heartbeat
			_ = hb.DecodeBody(&body)
			for _, l := range body.Loaded {
				if l.Name == model {
					if got = l.NParallel; got == want {
						return got
					}
				}
			}
		}
		return got
	}
	if res := hc.action(t, "models.load", `{"model":"fake-llm-8b-q4"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	if got := slots("fake-llm-8b-q4", 1); got != 1 {
		t.Errorf("per-model slots = %d, want 1", got)
	}
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	if got := slots("fake-llm-3b-q8", 2); got != 2 {
		t.Errorf("node default slots = %d, want 2", got)
	}
	if res := hc.action(t, "models.unload", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("unload: %+v", res.Error)
	}
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8","n_parallel":3}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	if got := slots("fake-llm-3b-q8", 3); got != 3 {
		t.Errorf("explicit slots = %d, want 3", got)
	}
	if res := hc.action(t, "models.load", `{"model":"fake-llm-8b-q4","n_parallel":99}`); res.OK || res.Error.Code != protocol.ErrInvalidArgs {
		t.Errorf("99 slots accepted: %+v", res)
	}
}

func TestActions(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, testConfig)

	// Successful load -> result, model_loaded event, immediate heartbeat.
	res := hc.action(t, "models.load", `{"model":"fake-llm-8b-q4","context_length":8192,"ttl_seconds":0}`)
	if !res.OK {
		t.Fatalf("load failed: %+v", res.Error)
	}
	data := hc.waitEvent(t, protocol.EventModelLoaded)
	if data["model"] != "fake-llm-8b-q4" || data["vram_used_mb"] != float64(6100) {
		t.Fatalf("model_loaded data %v", data)
	}
	var hbb protocol.Heartbeat
	hc.waitFor(t, "heartbeat with the loaded model", func(e protocol.Envelope) bool {
		hbb = protocol.Heartbeat{}
		return e.Type == protocol.TypeHeartbeat && e.DecodeBody(&hbb) == nil && len(hbb.Loaded) > 0
	})
	if len(hbb.Loaded) != 1 || hbb.Loaded[0].Name != "fake-llm-8b-q4" || hbb.GPUs[0].VRAMUsedMB != 6900 {
		t.Fatalf("heartbeat after load %+v", hbb)
	}

	errCases := []struct {
		name, args, code string
	}{
		{"models.delete", `{"model":"x","confirm":true}`, protocol.ErrNotAllowed}, // not in allowed_actions
		{"shell.exec", `{"cmd":"id"}`, protocol.ErrNotAllowed},
		{"models.download", `{"source":"x","target_name":"y"}`, protocol.ErrInvalidArgs},
		{"models.download", `{"source":{"type":"s3","repo":"a/b","file":"m.gguf"}}`, protocol.ErrInvalidArgs},
		{"models.download", `{"source":{"type":"huggingface","repo":"a/b","file":"../m.gguf"}}`, protocol.ErrInvalidArgs},
		{"models.load", `{"model":"fake-llm-3b-q8","foo":1}`, protocol.ErrInvalidArgs},
		{"models.load", `{"model":"fake-llm-3b-q8","context_length":0}`, protocol.ErrInvalidArgs},
		{"models.load", `{"model":"fake-llm-3b-q8","context_length":1.5}`, protocol.ErrInvalidArgs},
		{"models.load", `{"model":"fake-llm-3b-q8","ttl_seconds":-1}`, protocol.ErrInvalidArgs},
		{"models.load", `{"model":"fake-llm-3b-q8","context_length":"8192"}`, protocol.ErrInvalidArgs},
		{"models.load", `{"model":5}`, protocol.ErrInvalidArgs},
		{"models.load", `{}`, protocol.ErrInvalidArgs},
		{"models.unload", `{}`, protocol.ErrInvalidArgs},
		{"models.unload", `{"model":"a","all":true}`, protocol.ErrInvalidArgs},
		{"models.unload", `{"all":false}`, protocol.ErrInvalidArgs},
		{"models.unload", `{"all":true,"x":1}`, protocol.ErrInvalidArgs},
		{"models.list", `{"x":1}`, protocol.ErrInvalidArgs},
		{"models.load", `{"model":"no-such-model"}`, protocol.ErrRunnerError},
		{"models.load", `{"model":"fake-llm-14b-q4"}`, protocol.ErrRunnerError},  // out of fake VRAM
		{"models.unload", `{"model":"fake-llm-3b-q8"}`, protocol.ErrRunnerError}, // not loaded
	}
	for _, c := range errCases {
		res := hc.action(t, c.name, c.args)
		if res.OK || res.Error == nil || res.Error.Code != c.code {
			t.Errorf("%s %s: got %+v / %+v, want %s", c.name, c.args, res, res.Error, c.code)
		}
	}

	// models.list returns models + loaded and triggers a fresh inventory.
	res = hc.action(t, "models.list", `{}`)
	if !res.OK {
		t.Fatalf("list: %+v", res.Error)
	}
	m := res.Result.(map[string]any)
	if len(m["models"].([]any)) != 3 || len(m["loaded"].([]any)) != 1 {
		t.Fatalf("list result %v", m)
	}
	hc.waitType(t, protocol.TypeInventory)

	// Unload by name.
	res = hc.action(t, "models.unload", `{"model":"fake-llm-8b-q4"}`)
	if !res.OK {
		t.Fatalf("unload: %+v", res.Error)
	}
	data = hc.waitEvent(t, protocol.EventModelUnloaded)
	if data["model"] != "fake-llm-8b-q4" || data["reason"] != "action" {
		t.Fatalf("model_unloaded %v", data)
	}

	// Game starts -> busy_gaming, models unloaded; load is rejected with busy.
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	h.watcher.Start("GAME.exe")
	if d := hc.waitEvent(t, protocol.EventGameDetected); d["process"] != "GAME.exe" {
		t.Fatalf("game_detected %v", d)
	}
	if d := hc.waitEvent(t, protocol.EventStateChanged); d["from"] != "available" || d["to"] != "busy_gaming" {
		t.Fatalf("state_changed %v", d)
	}
	if d := hc.waitEvent(t, protocol.EventModelUnloaded); d["model"] != "fake-llm-3b-q8" || d["reason"] != "game" {
		t.Fatalf("model_unloaded %v", d)
	}
	res = hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`)
	if res.OK || res.Error.Code != protocol.ErrBusy {
		t.Fatalf("load while gaming: %+v %+v", res, res.Error)
	}
	h.watcher.Stop("GAME.exe")
	hc.waitEvent(t, protocol.EventGameEnded)
	if d := hc.waitEvent(t, protocol.EventStateChanged); d["to"] != "available" {
		t.Fatalf("state_changed %v", d)
	}
	if n := loadedNames(t, h.world); len(n) != 0 {
		t.Fatalf("models reloaded automatically: %v", n)
	}
	// Unload all.
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	res = hc.action(t, "models.unload", `{"all":true}`)
	if !res.OK || len(res.Result.(map[string]any)["unloaded"].([]any)) != 1 {
		t.Fatalf("unload all %+v", res)
	}
}

func TestReconnectAfterDropAndBackoffReset(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, testConfig)

	// Hub drops the connection.
	_ = hc.c.Close(websocket.StatusGoingAway, "hub restart")

	// The session had completed hello/config, so the backoff starts again at Min.
	select {
	case d := <-h.sleeps:
		if d != 10*time.Millisecond {
			t.Fatalf("first reconnect delay %v, want 10ms (reset)", d)
		}
	case <-time.After(waitTimeout):
		t.Fatal("no reconnect scheduled")
	}
	hc2 := hub.accept(t)
	env := hc2.next(t)
	var hello protocol.Hello
	_ = env.DecodeBody(&hello)
	want := configBody(t, testConfig).ConfigHash
	if env.Type != protocol.TypeHello || hello.ConfigHash == nil || *hello.ConfigHash != want {
		t.Fatalf("hello after reconnect: %s %+v", env.Type, hello)
	}
}

func TestBackoffGrowsWhileHubDown(t *testing.T) {
	h := newHarness(t, downURL(t), t.TempDir(), fake.DefaultProfile())
	h.start(t)
	want := []time.Duration{10, 20, 40, 80, 80}
	for i, w := range want {
		select {
		case d := <-h.sleeps:
			if d != w*time.Millisecond {
				t.Fatalf("attempt %d: delay %v want %v", i, d, w*time.Millisecond)
			}
		case <-time.After(waitTimeout):
			t.Fatal("timeout")
		}
	}
}

func TestRejectedCredentialUsesMaxBackoff(t *testing.T) {
	hub := newFakeHub(t)
	hub.reject.Store(true)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	for i := 0; i < 3; i++ {
		select {
		case d := <-h.sleeps:
			if d != 80*time.Millisecond {
				t.Fatalf("delay after 403: %v, want max", d)
			}
		case <-time.After(waitTimeout):
			t.Fatal("timeout")
		}
	}
	if !strings.Contains(h.logs.String(), "hub rejected the node identity") {
		t.Fatal("rejection not logged clearly")
	}
	_, err := h.agent.runSession(context.Background())
	if !errIsUnauthorized(err) {
		t.Fatalf("runSession error %v", err)
	}
}

func writeCachedConfig(t *testing.T, dir string, cfg string) string {
	t.Helper()
	d, err := store.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	hash, _ := protocol.ConfigHash([]byte(cfg))
	if err := d.SaveConfig(hash, json.RawMessage(cfg), time.Now()); err != nil {
		t.Fatal(err)
	}
	return hash
}

func TestOfflineCachedConfigAndQueueReplayAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	hash := writeCachedConfig(t, dir, testConfig)
	profile := fake.DefaultProfile()
	profile.Loaded = []string{"fake-llm-8b-q4"}

	// Phase 1: hub unreachable. The cached config drives the game watcher.
	h1 := newHarness(t, downURL(t), dir, profile)
	h1.start(t)
	eventually(t, "watcher subscription", func() bool { return h1.watcher.Subscribers() == 1 })
	h1.watcher.Start("game.exe")
	eventually(t, "models unloaded for game", func() bool { return len(loadedNames(t, h1.world)) == 0 })
	eventually(t, "busy_gaming", func() bool { return h1.agent.State() == protocol.StateBusyGaming })
	eventually(t, "3 queued events", func() bool { return h1.agent.queue.Len() == 3 })
	queued := h1.agent.queue.Snapshot()
	h1.stop()

	kinds := []string{}
	for _, e := range queued {
		var ev protocol.Event
		_ = e.DecodeBody(&ev)
		kinds = append(kinds, ev.Kind)
	}
	if strings.Join(kinds, ",") != "game_detected,state_changed,model_unloaded" {
		t.Fatalf("queued kinds %v", kinds)
	}

	// Phase 2: restart with a reachable hub; the queue is replayed from disk.
	hub := newFakeHub(t)
	h2 := newHarness(t, hub.srv.URL, dir, fake.DefaultProfile())
	if h2.agent.queue.Len() != 3 {
		t.Fatalf("queue not persisted: %d", h2.agent.queue.Len())
	}
	h2.start(t)
	hc := hub.accept(t)
	env := hc.next(t)
	var hello protocol.Hello
	_ = env.DecodeBody(&hello)
	if hello.ConfigHash == nil || *hello.ConfigHash != hash {
		t.Fatalf("hello config_hash %v want %s", hello.ConfigHash, hash)
	}
	hc.send(t, protocol.TypeConfig, configBody(t, testConfig))
	if env := hc.next(t); env.Type != protocol.TypeInventory {
		t.Fatalf("got %s want inventory", env.Type)
	}
	env = hc.next(t)
	if env.Type != protocol.TypeQueuedEvents {
		t.Fatalf("got %s want queued_events", env.Type)
	}
	var qe protocol.QueuedEvents
	_ = env.DecodeBody(&qe)
	if len(qe.Events) != 3 {
		t.Fatalf("replayed %d events", len(qe.Events))
	}
	for i := range qe.Events {
		if qe.Events[i].ID != queued[i].ID || qe.Events[i].TS != queued[i].TS {
			t.Fatalf("event %d changed id/ts: %+v vs %+v", i, qe.Events[i], queued[i])
		}
	}
	if env := hc.next(t); env.Type != protocol.TypeHeartbeat {
		t.Fatalf("got %s want heartbeat", env.Type)
	}
	eventually(t, "queue cleared", func() bool { return h2.agent.queue.Len() == 0 })
	q, _, err := store.OpenQueue(h2.dir.QueuePath(), 0)
	if err != nil || q.Len() != 0 {
		t.Fatalf("queue file not cleared: %v %d", err, q.Len())
	}
}

func TestUpgradeRequiredOnlyHeartbeats(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), func(o *Options) {
		o.HeartbeatInterval = 20 * time.Millisecond
		o.HandshakeTimeout = 100 * time.Millisecond
	})
	h.start(t)
	hc := hub.accept(t)
	hc.next(t) // hello
	hc.send(t, protocol.TypeError, protocol.Error{Code: protocol.ErrUpgradeRequired, Message: "protocol version 0 is not supported"})
	hc.waitType(t, protocol.TypeHeartbeat)
	hc.send(t, protocol.TypeAction, map[string]any{"name": "models.list", "args": map[string]any{}})
	// Stays connected past the handshake timeout, sends nothing but heartbeats.
	deadline := time.After(300 * time.Millisecond)
	beats := 0
	for done := false; !done; {
		select {
		case env := <-hc.msgs:
			if env.Type != protocol.TypeHeartbeat {
				t.Fatalf("sent %s after upgrade_required", env.Type)
			}
			beats++
		case <-deadline:
			done = true
		}
	}
	if beats < 3 {
		t.Fatalf("only %d heartbeats", beats)
	}
	select {
	case <-hc.closed:
		t.Fatal("connection closed after upgrade_required")
	default:
	}
}

func TestConfigHashMismatchIsWarningOnly(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	hc := hub.accept(t)
	hc.next(t)
	wrong := strings.Repeat("0", 64)
	hc.send(t, protocol.TypeConfig, protocol.ConfigBody{ConfigHash: wrong, Config: json.RawMessage(testConfig)})
	hc.waitType(t, protocol.TypeInventory)
	if !strings.Contains(h.logs.String(), "config_hash mismatch") {
		t.Fatal("mismatch not logged")
	}
	cc, _, _ := h.dir.LoadConfig()
	if cc.ConfigHash != wrong {
		t.Fatalf("cached hash %s", cc.ConfigHash)
	}
}

func TestInvalidConfigRejected(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), func(o *Options) { o.HandshakeTimeout = 200 * time.Millisecond })
	h.start(t)
	hc := hub.accept(t)
	hc.next(t)
	// Schema-valid but rejected by the node (whitespace-only process name).
	bad := strings.Replace(testConfig, `"process":"game.exe"`, `"process":"  "`, 1)
	hc.send(t, protocol.TypeConfig, configBody(t, bad))
	// Not applied, not cached; the handshake times out and the node reconnects.
	hc2 := hub.accept(t)
	if env := hc2.next(t); env.Type != protocol.TypeHello {
		t.Fatalf("got %s", env.Type)
	}
	if _, ok, _ := h.dir.LoadConfig(); ok {
		t.Fatal("invalid config was cached")
	}
}

func TestWSURL(t *testing.T) {
	cases := map[string]string{
		"http://hub.example.lan:8000":        "ws://hub.example.lan:8000/api/node/ws",
		"http://hub.example.lan:8000/":       "ws://hub.example.lan:8000/api/node/ws",
		"https://hub.example.lan/topcoat/":   "wss://hub.example.lan/topcoat/api/node/ws",
		"ws://hub.example.lan:8000?x=1#frag": "ws://hub.example.lan:8000/api/node/ws",
	}
	for in, want := range cases {
		got, err := WSURL(in)
		if err != nil || got != want {
			t.Errorf("%s: got %s %v want %s", in, got, err, want)
		}
	}
	for _, bad := range []string{"ftp://hub.example.lan", "hub.example.lan:8000", "http://", "::"} {
		if _, err := WSURL(bad); err == nil {
			t.Errorf("%s accepted", bad)
		}
	}
	if errIsUnauthorized(errors.New("x")) {
		t.Fatal("plain error treated as unauthorized")
	}
}

func TestWrongKeyIsRejected(t *testing.T) {
	hub := newFakeHub(t)
	other := ed25519.NewKeyFromSeed(bytes.Repeat([]byte{9}, ed25519.SeedSize))
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), func(o *Options) { o.Key = other })
	_, err := h.agent.runSession(context.Background())
	if !errIsUnauthorized(err) {
		t.Fatalf("runSession with a foreign key: %v", err)
	}
}

func TestChallengeFailureIsReported(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	t.Cleanup(srv.Close)
	h := newHarness(t, srv.URL, t.TempDir(), fake.DefaultProfile())
	_, err := h.agent.runSession(context.Background())
	if err == nil || errIsUnauthorized(err) || !strings.Contains(err.Error(), "challenge") {
		t.Fatalf("got %v", err)
	}
}

func TestNewRequiresKey(t *testing.T) {
	_, err := New(Options{Dir: &store.Dir{Path: t.TempDir()}})
	if err == nil || !strings.Contains(err.Error(), "Key") {
		t.Fatal(err)
	}
}
