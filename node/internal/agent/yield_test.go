package agent

import (
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/fake"
)

// yieldConfig turns the local-use handling on with fast timers.
func yieldConfig(extra string) string {
	return strings.Replace(proxyConfig, `"capabilities":[]`,
		`"yield":{"enabled":true,"poll_seconds":1,"idle_seconds":60,"cpu_percent":70,"sustain_seconds":0,"resume_seconds":0`+extra+`},"capabilities":[]`, 1)
}

func yieldSetup(t *testing.T, cfg string) (*harness, *hubConn) {
	t.Helper()
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), func(o *Options) {
		o.YieldPoll = 10 * time.Millisecond
	})
	h.world.StreamDelay = 20 * time.Millisecond
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, cfg)
	return h, hc
}

// waitState waits for a heartbeat that reports state.
func waitNodeState(t *testing.T, hc *hubConn, state string) {
	t.Helper()
	hc.waitFor(t, "heartbeat with state "+state, func(e protocol.Envelope) bool {
		var hb protocol.Heartbeat
		return e.Type == protocol.TypeHeartbeat && e.DecodeBody(&hb) == nil && hb.State == state
	})
}

func TestTheNodePausesForInputAndContinuesWhenTheUserIsGone(t *testing.T) {
	h, hc := yieldSetup(t, yieldConfig(""))
	h.world.SetInputIdle(time.Hour, true) // nobody there
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}

	h.world.SetInputIdle(0, true) // a key was pressed
	d := hc.waitEvent(t, protocol.EventPaused)
	if rs, _ := d["reasons"].([]any); len(rs) != 1 || rs[0] != protocol.PauseInput || d["models"] != float64(1) {
		t.Fatalf("paused %v", d)
	}
	waitNodeState(t, hc, protocol.StatePaused)
	if !h.world.Frozen() {
		t.Fatal("the model processes must be frozen")
	}
	// new work is refused while paused: gateway requests (the hub asks another node) and loads
	hc.send(t, protocol.TypeProxyRequest, proxyReq(streamID, "/v1/chat/completions", "POST", chatBody(false)))
	_, end := collect(t, hc, streamID)
	if end.Status != 503 || end.Error == nil || end.Error.Code != protocol.ErrBusy {
		t.Fatalf("proxy while paused: %+v", end)
	}
	if res := hc.action(t, "models.load", `{"model":"fake-llm-8b-q4"}`); res.OK || res.Error.Code != protocol.ErrBusy {
		t.Fatalf("load while paused: %+v", res)
	}

	h.world.SetInputIdle(time.Hour, true) // the user went away
	d = hc.waitEvent(t, protocol.EventResumed)
	if rs, _ := d["reasons"].([]any); len(rs) != 1 || rs[0] != protocol.PauseInput {
		t.Fatalf("resumed %v", d)
	}
	waitNodeState(t, hc, protocol.StateAvailable)
	if h.world.Frozen() {
		t.Fatal("the model processes must continue")
	}
	// the model is still loaded and answers again: nothing was lost
	hc.send(t, protocol.TypeProxyRequest, proxyReq(streamID, "/v1/chat/completions", "POST", chatBody(false)))
	chunks, end := collect(t, hc, streamID)
	if end.Status != 200 || len(chunks) == 0 {
		t.Fatalf("after resume: %+v", end)
	}
	if pids := h.world.OwnPIDs(); len(pids) == 0 {
		t.Fatal("the node must name its own processes so their CPU use is not foreign load")
	}
}

func TestARequestInFlightIsToldItIsPausedAndFinishesAfterwards(t *testing.T) {
	h, hc := yieldSetup(t, yieldConfig(""))
	h.world.SetInputIdle(time.Hour, true)
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	// a long streamed answer
	body := strings.Replace(string(chatBody(true)), "Hallo Welt", strings.Repeat("wort ", 60), 1)
	hc.send(t, protocol.TypeProxyRequest, proxyReq(streamID, "/v1/chat/completions", "POST", []byte(body)))
	hc.waitFor(t, "first chunk", func(e protocol.Envelope) bool { return e.Type == protocol.TypeProxyChunk })

	h.world.SetInputIdle(0, true)
	var st protocol.ProxyStatus
	hc.waitFor(t, "proxy_status paused", func(e protocol.Envelope) bool {
		return e.Type == protocol.TypeProxyStatus && e.DecodeBody(&st) == nil && st.State == protocol.ProxyPaused
	})
	if st.StreamID != streamID || len(st.Reasons) != 1 || st.Reasons[0] != protocol.PauseInput {
		t.Fatalf("%+v", st)
	}
	// the stream stands still (at most the chunk that was on its way)
	time.Sleep(150 * time.Millisecond)
	n := drainChunks(hc)
	time.Sleep(150 * time.Millisecond)
	if more := drainChunks(hc); more != 0 {
		t.Fatalf("a frozen model kept answering (%d then %d chunks)", n, more)
	}

	h.world.SetInputIdle(time.Hour, true)
	hc.waitFor(t, "proxy_status running", func(e protocol.Envelope) bool {
		return e.Type == protocol.TypeProxyStatus && e.DecodeBody(&st) == nil && st.State == protocol.ProxyRunning
	})
	// and it finishes at the same place, without an error
	_, end := collect(t, hc, streamID)
	if end.Status != 200 || end.Error != nil {
		t.Fatalf("the answer must finish after the pause: %+v", end)
	}
}

// drainChunks counts the proxy_chunk messages that arrive within a moment.
func drainChunks(hc *hubConn) int {
	n := 0
	for {
		select {
		case e := <-hc.msgs:
			if e.Type == protocol.TypeProxyChunk {
				n++
			}
		case <-time.After(20 * time.Millisecond):
			return n
		}
	}
}

func TestAPauseThatLastsUnloadsTheModelsAndTheyCanComeBack(t *testing.T) {
	cfg := yieldConfig(`,"unload_after_pause_seconds":1`)
	// the model is a resident: it comes back on its own after the pause
	cfg = strings.Replace(cfg, `"capabilities":[]`, `"residents":[{"model":"fake-llm-3b-q8"}],"pressure":{"poll_seconds":1,"reload_after_seconds":0,"min_off_seconds":0},"capabilities":[]`, 1)
	h, hc := yieldSetup(t, cfg)
	h.world.SetInputIdle(time.Hour, true)
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	h.world.SetInputIdle(0, true)
	hc.waitEvent(t, protocol.EventPaused)
	d := hc.waitEvent(t, protocol.EventModelUnloaded)
	if d["model"] != "fake-llm-3b-q8" || d["reason"] != "pause_timeout" {
		t.Fatalf("model_unloaded %v", d)
	}
	if n := loadedNames(t, h.world); len(n) != 0 {
		t.Fatalf("still loaded: %v", n)
	}
	// nothing is loaded behind the user's back while the pause lasts
	time.Sleep(300 * time.Millisecond)
	if n := loadedNames(t, h.world); len(n) != 0 {
		t.Fatalf("a resident was reloaded during the pause: %v", n)
	}
	h.world.SetInputIdle(time.Hour, true)
	hc.waitEvent(t, protocol.EventResumed)
	deadline := time.Now().Add(5 * time.Second)
	for len(loadedNames(t, h.world)) == 0 {
		if time.Now().After(deadline) {
			t.Fatal("the resident did not come back after the pause")
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func TestForeignCPULoadAndBatteryPauseToo(t *testing.T) {
	h, hc := yieldSetup(t, yieldConfig(""))
	h.world.SetInputIdle(time.Hour, true)
	h.world.SetForeignCPU(95, true)
	if d := hc.waitEvent(t, protocol.EventPaused); d["reasons"].([]any)[0] != protocol.PauseCPULoad {
		t.Fatalf("%v", d)
	}
	h.world.SetForeignCPU(5, true)
	hc.waitEvent(t, protocol.EventResumed)
	h.world.SetOnBattery(true, true)
	if d := hc.waitEvent(t, protocol.EventPaused); d["reasons"].([]any)[0] != protocol.PauseBattery {
		t.Fatalf("%v", d)
	}
}

func TestANodeWithADedicatedGPUDoesNotPauseByDefault(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), func(o *Options) { o.YieldPoll = 10 * time.Millisecond })
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, proxyConfig) // no yield object: the default depends on the GPU
	h.world.SetInputIdle(0, true)
	time.Sleep(300 * time.Millisecond)
	if h.agent.State() == protocol.StatePaused || h.agent.isPaused() {
		t.Fatal("the fast machine with a dedicated GPU must not duck by default")
	}
}

func TestANodeWithoutADedicatedGPUPausesByDefault(t *testing.T) {
	hub := newFakeHub(t)
	p := fake.DefaultProfile()
	p.GPUs = []fake.ProfileGPU{{Name: "Iris Xe", Vendor: "intel", VRAMTotalMB: 128, Integrated: true, SharedTotalMB: 16384}}
	p.Models = []fake.ProfileModel{{Name: "fake-llm-3b-q8", SizeBytes: 3_400_000_000, Format: "gguf", VRAMMB: 3600}}
	h := newHarness(t, hub.srv.URL, t.TempDir(), p, func(o *Options) { o.YieldPoll = 10 * time.Millisecond })
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, proxyConfig)
	h.world.SetInputIdle(0, true)
	hc.waitEvent(t, protocol.EventPaused)
}

func TestActivityThatIsNotKnownNeverPauses(t *testing.T) {
	h, hc := yieldSetup(t, yieldConfig(""))
	h.world.SetInputErr("cannot see the console session")
	time.Sleep(300 * time.Millisecond)
	if h.agent.isPaused() {
		t.Fatal("a node that sees nothing keeps working")
	}
	deadline := time.Now().Add(2 * time.Second)
	for !strings.Contains(h.logs.String(), "cannot see keyboard/mouse input") {
		if time.Now().After(deadline) {
			t.Fatal("the reason must be logged once")
		}
		time.Sleep(20 * time.Millisecond)
	}
	if strings.Count(h.logs.String(), "cannot see keyboard/mouse input") != 1 {
		t.Fatal("logged more than once")
	}
	_ = hc
}

func TestTheCPUMeasuresAreLoggedOnceAndAgainWhenTheyChange(t *testing.T) {
	h, _ := yieldSetup(t, yieldConfig(""))
	const line = "CPU load of other programs is measured with"
	wait := func(n int) {
		t.Helper()
		deadline := time.Now().Add(2 * time.Second)
		for strings.Count(h.logs.String(), line) < n {
			if time.Now().After(deadline) {
				t.Fatalf("expected %d log lines, log:\n%s", n, h.logs.String())
			}
			time.Sleep(20 * time.Millisecond)
		}
	}
	h.world.SetForeignCPU(5, true)
	h.world.SetCPUNote("time, utility; not available: hyperv: 0x800007d5")
	wait(1)
	time.Sleep(200 * time.Millisecond) // many samples with the same note
	if got := strings.Count(h.logs.String(), line); got != 1 {
		t.Fatalf("logged %d times", got)
	}
	h.world.SetCPUNote("time, utility, hyperv")
	wait(2)
}

func TestAnAnswerCutOffByAnUnloadEndsWithInterrupted(t *testing.T) {
	// the hub continues such an answer (Continue logic) instead of calling it an error
	h, hc := proxySetup(t, proxyConfig)
	// proxySetup streams without a pause; the answer must still be running when the unload comes
	h.world.StreamDelay = 20 * time.Millisecond
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	body := strings.Replace(string(chatBody(true)), "Hallo Welt", strings.Repeat("wort ", 200), 1)
	hc.send(t, protocol.TypeProxyRequest, proxyReq(streamID, "/v1/chat/completions", "POST", []byte(body)))
	hc.waitFor(t, "first chunk", func(e protocol.Envelope) bool { return e.Type == protocol.TypeProxyChunk })
	if res := hc.action(t, "models.unload", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("unload: %+v", res.Error)
	}
	_, end := collect(t, hc, streamID)
	if end.Error == nil || end.Error.Code != protocol.ErrInterrupted {
		t.Fatalf("the cut-off answer must say why: %+v", end)
	}
}
