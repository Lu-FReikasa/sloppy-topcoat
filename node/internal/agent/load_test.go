package agent

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/fake"
)

// sendLoad sends a models.load action without waiting for its result.
func sendLoad(t *testing.T, hc *hubConn, model string) string {
	t.Helper()
	return hc.send(t, protocol.TypeAction, map[string]any{"name": "models.load", "args": json.RawMessage(`{"model":"` + model + `"}`)})
}

func resultFor(t *testing.T, hc *hubConn, id string) protocol.ActionResult {
	t.Helper()
	env := hc.wait(t, "action_result "+id, func(e protocol.Envelope) bool {
		return e.Type == protocol.TypeActionResult && e.Re == id
	}, true)
	var res protocol.ActionResult
	if err := env.DecodeBody(&res); err != nil {
		t.Fatal(err)
	}
	return res
}

// countEvents drains what the node sends for a moment and counts events of kind.
func countEvents(hc *hubConn, kind string, window time.Duration) int {
	n := 0
	for _, e := range hc.stash {
		n += eventOfKind(e, kind)
	}
	hc.stash = nil
	deadline := time.After(window)
	for {
		select {
		case e := <-hc.msgs:
			n += eventOfKind(e, kind)
		case <-deadline:
			return n
		}
	}
}

func eventOfKind(e protocol.Envelope, kind string) int {
	if e.Type != protocol.TypeEvent {
		return 0
	}
	var ev protocol.Event
	if e.DecodeBody(&ev) == nil && ev.Kind == kind {
		return 1
	}
	return 0
}

func TestConcurrentLoadsOfOneModelAreOneLoadAndOneEvent(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.world.LoadDelay = 300 * time.Millisecond
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, testConfig)

	// an action and two more requests (a chat asks for the model too) for the same model
	ids := []string{sendLoad(t, hc, "fake-llm-8b-q4"), sendLoad(t, hc, "fake-llm-8b-q4"), sendLoad(t, hc, "fake-llm-8b-q4")}
	for _, id := range ids {
		if res := resultFor(t, hc, id); !res.OK {
			t.Fatalf("load: %+v", res.Error)
		}
	}
	if h.world.Loads != 1 {
		t.Errorf("%d loads happened, want 1", h.world.Loads)
	}
	if n := countEvents(hc, protocol.EventModelLoaded, 200*time.Millisecond); n != 1 {
		t.Errorf("%d model_loaded events, want 1", n)
	}
	// asking for a model that is loaded already reports nothing new
	if res := hc.action(t, "models.load", `{"model":"fake-llm-8b-q4"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	if n := countEvents(hc, protocol.EventModelLoaded, 100*time.Millisecond); n != 0 {
		t.Errorf("%d model_loaded events for a model that was loaded already", n)
	}
}

func TestAFailedLoadIsAnEventWithTheReason(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.world.LoadFail = errors.New("llama_kv_cache: failed to allocate Vulkan0 buffer of size 805306368")
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, testConfig)
	res := hc.action(t, "models.load", `{"model":"fake-llm-8b-q4"}`)
	if res.OK || res.Error.Code != protocol.ErrRunnerError {
		t.Fatalf("%+v %+v", res, res.Error)
	}
	d := hc.waitEvent(t, protocol.EventModelLoadFailed)
	if d["model"] != "fake-llm-8b-q4" || d["code"] != protocol.ErrRunnerError || d["message"] == "" {
		t.Fatalf("model_load_failed %v", d)
	}
	if msg, _ := d["message"].(string); !strings.Contains(msg, "failed to allocate") {
		t.Errorf("the event carries the reason: %q", msg)
	}
}

func TestUnloadAllCancelsALoadInProgress(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.world.LoadDelay = 30 * time.Second // "forever"
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, testConfig)

	id := sendLoad(t, hc, "fake-llm-8b-q4")
	hc.wait(t, "heartbeat that says the model is loading", func(e protocol.Envelope) bool {
		var hb protocol.Heartbeat
		return e.Type == protocol.TypeHeartbeat && e.DecodeBody(&hb) == nil && len(hb.Loading) > 0
	}, true)
	if res := hc.action(t, "models.unload", `{"all":true}`); !res.OK {
		t.Fatalf("unload all: %+v", res.Error)
	}
	res := resultFor(t, hc, id)
	if res.OK || res.Error.Code != protocol.ErrCanceled {
		t.Fatalf("the canceled load must say so: %+v %+v", res, res.Error)
	}
	d := hc.waitEvent(t, protocol.EventModelLoadCancelled)
	if d["model"] != "fake-llm-8b-q4" || d["reason"] != "unload_all" {
		t.Fatalf("model load cancel event %v", d)
	}
	if n := countEvents(hc, protocol.EventModelLoadFailed, 50*time.Millisecond); n != 0 {
		t.Errorf("a load canceled on purpose is no failure (%d model_load_failed)", n)
	}
	if n := countEvents(hc, protocol.EventModelLoaded, 100*time.Millisecond); n != 0 {
		t.Errorf("a canceled load must not report model_loaded (%d)", n)
	}
	if names := loadedNames(t, h.world); len(names) != 0 {
		t.Errorf("model appeared after unload all: %v", names)
	}
	// the slot is free again: the same model can be loaded afterwards
	h.world.LoadDelay = 0
	if res := hc.action(t, "models.load", `{"model":"fake-llm-8b-q4"}`); !res.OK {
		t.Fatalf("load after cancel: %+v", res.Error)
	}
}

func TestTheResultOfALoadSurvivesADroppedSession(t *testing.T) {
	// The hub stopped waiting (here: the session ended) long before the 30B model was ready.
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.world.LoadDelay = 400 * time.Millisecond
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, testConfig)
	sendLoad(t, hc, "fake-llm-8b-q4")
	hc.wait(t, "loading heartbeat", func(e protocol.Envelope) bool {
		var hb protocol.Heartbeat
		return e.Type == protocol.TypeHeartbeat && e.DecodeBody(&hb) == nil && len(hb.Loading) > 0
	}, true)
	_ = hc.c.CloseNow()

	hc2 := hub.accept(t)
	hc2.next(t) // hello
	hc2.send(t, protocol.TypeConfig, configBody(t, testConfig))
	found := false
	hc2.waitFor(t, "model_loaded, live or replayed from the queue", func(e protocol.Envelope) bool {
		if eventOfKind(e, protocol.EventModelLoaded) == 1 {
			found = true
		}
		if e.Type == protocol.TypeQueuedEvents {
			var qe protocol.QueuedEvents
			_ = e.DecodeBody(&qe)
			for _, ev := range qe.Events {
				found = found || eventOfKind(ev, protocol.EventModelLoaded) == 1
			}
		}
		return found
	})
	if names := loadedNames(t, h.world); len(names) != 1 {
		t.Errorf("the load must finish without the session: %v", names)
	}
}

func TestUnloadingOneModelCancelsItsLoadAndSaysWhy(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.world.LoadDelay = 30 * time.Second
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, testConfig)
	id := sendLoad(t, hc, "fake-llm-8b-q4")
	hc.wait(t, "loading heartbeat", func(e protocol.Envelope) bool {
		var hb protocol.Heartbeat
		return e.Type == protocol.TypeHeartbeat && e.DecodeBody(&hb) == nil && len(hb.Loading) > 0
	}, true)
	start := time.Now()
	hc.send(t, protocol.TypeAction, map[string]any{"name": "models.unload", "args": json.RawMessage(`{"model":"fake-llm-8b-q4"}`)})
	if res := resultFor(t, hc, id); res.OK || res.Error.Code != protocol.ErrCanceled {
		t.Fatalf("%+v %+v", res, res.Error)
	}
	if time.Since(start) > 3*time.Second {
		t.Fatalf("canceling must not wait for the load to finish (%v)", time.Since(start))
	}
	if d := hc.waitEvent(t, protocol.EventModelLoadCancelled); d["reason"] != "unload" {
		t.Fatalf("%v", d)
	}
	if names := loadedNames(t, h.world); len(names) != 0 {
		t.Fatalf("still loaded: %v", names)
	}
}

// loadedContext is the context length in the result of models.load.
func loadedContext(t *testing.T, res protocol.ActionResult) int {
	t.Helper()
	out, _ := res.Result.(map[string]any)
	loaded, _ := out["loaded"].(map[string]any)
	n, ok := loaded["context_length"].(float64)
	if !ok {
		t.Fatalf("no context_length in %+v", res.Result)
	}
	return int(n)
}

func TestALoadThatAsksForAtLeastAContextLetsTheRunnerSizeIt(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, testConfig)

	res := hc.action(t, "models.load", `{"model":"fake-llm-8b-q4","min_context_length":16384}`)
	if !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	// the fake sizes like a roomy machine: twice the floor; never below what was asked
	if n := loadedContext(t, res); n != 32768 {
		t.Fatalf("context %d, want 32768", n)
	}
}

func TestAnExplicitContextWinsOverTheMinimum(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, testConfig)

	res := hc.action(t, "models.load", `{"model":"fake-llm-8b-q4","context_length":4096,"min_context_length":16384}`)
	if !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	if n := loadedContext(t, res); n != 4096 {
		t.Fatalf("context %d, want the explicit 4096", n)
	}
}

func TestABadMinimumIsInvalidArgs(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, testConfig)

	res := hc.action(t, "models.load", `{"model":"fake-llm-8b-q4","min_context_length":0}`)
	if res.OK || res.Error == nil || res.Error.Code != protocol.ErrInvalidArgs {
		t.Fatalf("got %+v", res)
	}
}
