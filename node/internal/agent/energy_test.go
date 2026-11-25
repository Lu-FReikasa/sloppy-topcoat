package agent

import (
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/fake"
)

// sleepConfig allows node.sleep and sets the power section.
func sleepConfig(power string) string {
	cfg := strings.Replace(proxyConfig, `"models.proxy"`, `"models.proxy","node.sleep"`, 1)
	return strings.Replace(cfg, `"capabilities":[]`, `"power":`+power+`,"capabilities":[]`, 1)
}

func energySetup(t *testing.T, cfg string, mods ...func(*Options)) (*harness, *hubConn) {
	t.Helper()
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), append(mods, func(o *Options) {
		o.SleepCheck = 20 * time.Millisecond
		o.SleepDelay = 30 * time.Millisecond
	})...)
	h.world.StreamDelay = 20 * time.Millisecond
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, cfg)
	return h, hc
}

func waitSlept(t *testing.T, h *harness, n int) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for h.world.Slept() < n {
		if time.Now().After(deadline) {
			t.Fatalf("the machine did not go to sleep (%d)", h.world.Slept())
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestTheInventoryListsThePhysicalNetworkInterfacesForWakeOnLAN(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	hc := hub.accept(t)
	env := hc.next(t) // hello
	_ = env
	hc.send(t, protocol.TypeConfig, configBody(t, testConfig))
	inv := hc.next(t)
	var body protocol.Inventory
	if err := inv.DecodeBody(&body); err != nil || inv.Type != protocol.TypeInventory {
		t.Fatalf("%s %v", inv.Type, err)
	}
	n := body.Hardware.Network
	if len(n) != 2 || n[0].MAC != "02:00:00:00:00:01" || n[0].Kind != "ethernet" {
		t.Fatalf("%+v", n)
	}
}

func TestNodeSleepIsNotAllowedByDefaultAndSleepsOnRequest(t *testing.T) {
	h, hc := energySetup(t, sleepConfig(`{"profile":"balanced"}`))
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	res := hc.action(t, "node.sleep", `{}`)
	if !res.OK {
		t.Fatalf("sleep: %+v", res.Error)
	}
	if d := hc.waitEvent(t, protocol.EventSleeping); d["reason"] != "action" {
		t.Fatalf("%v", d)
	}
	waitSlept(t, h, 1)
	if n := loadedNames(t, h.world); len(n) != 0 {
		t.Fatalf("the models must be unloaded before the machine sleeps: %v", n)
	}
}

func TestNodeSleepNeedsThePermission(t *testing.T) {
	h, hc := energySetup(t, proxyConfig) // node.sleep is not in allowed_actions
	if res := hc.action(t, "node.sleep", `{}`); res.OK || res.Error.Code != protocol.ErrNotAllowed {
		t.Fatalf("%+v", res)
	}
	time.Sleep(100 * time.Millisecond)
	if h.world.Slept() != 0 {
		t.Fatal("slept without permission")
	}
}

func TestNodeSleepRefusesWhileAnAnswerIsBeingGenerated(t *testing.T) {
	h, hc := energySetup(t, sleepConfig(`{}`))
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	body := strings.Replace(string(chatBody(true)), "Hallo Welt", strings.Repeat("wort ", 100), 1)
	hc.send(t, protocol.TypeProxyRequest, proxyReq(streamID, "/v1/chat/completions", "POST", []byte(body)))
	hc.waitFor(t, "first chunk", func(e protocol.Envelope) bool { return e.Type == protocol.TypeProxyChunk })
	res := hc.action(t, "node.sleep", `{}`)
	if res.OK || res.Error.Code != protocol.ErrBusy || !strings.Contains(res.Error.Message, "answer") {
		t.Fatalf("%+v %+v", res, res.Error)
	}
	if h.world.Slept() != 0 {
		t.Fatal("slept during an answer")
	}
}

func TestTheMachineSleepsByItselfAfterTheIdleTime(t *testing.T) {
	h, hc := energySetup(t, sleepConfig(`{"sleep_after_idle_seconds":1}`))
	h.world.SetInputIdle(time.Hour, true)
	d := hc.waitEvent(t, protocol.EventSleeping)
	if d["reason"] != "idle" {
		t.Fatalf("%v", d)
	}
	waitSlept(t, h, 1)
}

func TestTheMachineDoesNotSleepWhileSomeoneIsThereOrAModelIsLoaded(t *testing.T) {
	h, hc := energySetup(t, sleepConfig(`{"sleep_after_idle_seconds":1}`))
	h.world.SetInputIdle(0, true) // someone types
	time.Sleep(1500 * time.Millisecond)
	if h.world.Slept() != 0 {
		t.Fatal("slept while somebody was at the machine")
	}
	h.world.SetInputIdle(time.Hour, true)
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	time.Sleep(1500 * time.Millisecond)
	if h.world.Slept() != 0 {
		t.Fatal("slept with a model loaded")
	}
}

func TestOnlyAConfiguredIdleTimeMakesTheMachineSleep(t *testing.T) {
	h, _ := energySetup(t, sleepConfig(`{"profile":"saver"}`)) // no sleep_after_idle_seconds
	h.world.SetInputIdle(time.Hour, true)
	time.Sleep(400 * time.Millisecond)
	if h.world.Slept() != 0 {
		t.Fatal("the machine must never sleep by itself unless asked to")
	}
}

func TestPollingBacksOffWhileIdleAndNeverWithThePerformanceProfile(t *testing.T) {
	// balanced (the default): x3 while nothing is loaded, 1x with a model
	h, hc := energySetup(t, proxyConfig, func(o *Options) { o.YieldPoll = 0 })
	if got := h.agent.yieldPoll(); got != 15*time.Second {
		t.Fatalf("idle balanced poll %v, want 15s (5 s x3)", got)
	}
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res.Error)
	}
	if got := h.agent.yieldPoll(); got != 5*time.Second {
		t.Fatalf("with a model loaded the node looks at the normal rate: %v", got)
	}

	h2, _ := energySetup(t, sleepConfig(`{"profile":"performance"}`), func(o *Options) { o.YieldPoll = 0 })
	if got := h2.agent.yieldPoll(); got != 5*time.Second {
		t.Fatalf("performance never backs off: %v", got)
	}
	h3, _ := energySetup(t, sleepConfig(`{"profile":"saver"}`), func(o *Options) { o.YieldPoll, o.HeartbeatInterval = 0, 0 })
	if got := h3.agent.yieldPoll(); got != 30*time.Second {
		t.Fatalf("saver x6: %v", got)
	}
	if got := h3.agent.heartbeatInterval(); got != 30*time.Second { // 10 s x3 while idle
		t.Fatalf("saver heartbeat %v", got)
	}
}
