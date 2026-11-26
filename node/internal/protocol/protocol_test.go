package protocol_test

import (
	"bytes"
	"crypto/ed25519"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/schematest"
)

func exampleFiles(t *testing.T, kind string) []string {
	t.Helper()
	files, err := filepath.Glob(filepath.Join(schematest.ProtoDir(), "examples", "v1", kind, "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(files) == 0 {
		t.Fatalf("no %s examples found", kind)
	}
	return files
}

// isHTTPSchema reports examples of plain HTTP bodies (pairing, challenge), not envelopes.
func isHTTPSchema(file string) bool {
	base := filepath.Base(file)
	return strings.HasPrefix(base, "pair_") || strings.HasPrefix(base, "challenge_")
}

func schemaFor(file string) string {
	base := filepath.Base(file)
	prefix, _, _ := strings.Cut(base, ".")
	return prefix
}

func TestExamplesValid(t *testing.T) {
	for _, f := range exampleFiles(t, "valid") {
		t.Run(filepath.Base(f), func(t *testing.T) {
			raw, err := os.ReadFile(f)
			if err != nil {
				t.Fatal(err)
			}
			if err := schematest.Validate(schemaFor(f), raw); err != nil {
				t.Fatalf("valid example rejected: %v", err)
			}
			// Hub->node / node->hub envelopes must also decode with our types.
			if !isHTTPSchema(f) {
				var env protocol.Envelope
				if err := json.Unmarshal(raw, &env); err != nil {
					t.Fatal(err)
				}
				if err := schematest.ValidateValue(schemaFor(f), env); err != nil {
					t.Fatalf("round trip through Envelope breaks schema: %v", err)
				}
				if err := schematest.Validate("message", raw); err != nil {
					t.Fatalf("message.schema.json rejects it: %v", err)
				}
			}
		})
	}
}

func TestExamplesInvalid(t *testing.T) {
	for _, f := range exampleFiles(t, "invalid") {
		t.Run(filepath.Base(f), func(t *testing.T) {
			raw, err := os.ReadFile(f)
			if err != nil {
				t.Fatal(err)
			}
			if err := schematest.Validate(schemaFor(f), raw); err == nil {
				t.Fatal("invalid example accepted")
			}
		})
	}
}

func TestCanonicalJSON(t *testing.T) {
	in := `{"b":1,"a":[1,2],"c":{"y":"x","x":true}}`
	got, err := protocol.CanonicalJSON([]byte(in))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != `{"a":[1,2],"b":1,"c":{"x":true,"y":"x"}}` {
		t.Fatalf("canonical form %s", got)
	}
	h, err := protocol.ConfigHash([]byte(in))
	if err != nil {
		t.Fatal(err)
	}
	// python3 -c 'import json,hashlib; print(hashlib.sha256(json.dumps({"b":1,"a":[1,2],"c":{"y":"x","x":True}},
	//   sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest())'
	const want = "e27d6ff7ab0c3b1ab0a5b49657dc074d31053a05d00b17954975e1086b3fe499"
	if h != want {
		t.Fatalf("hash %s want %s", h, want)
	}
}

func TestCanonicalJSONEscapingMatchesPython(t *testing.T) {
	// Expected values produced by Python json.dumps(..., sort_keys=True, separators=(",",":"), ensure_ascii=False).
	cases := map[string]string{
		`{"s":"a\"b\\c\n\t\r\b\f\u0001<&>"}`: `{"s":"a\"b\\c\n\t\r\b\f\u0001<&>"}`,
		`{"ü":"ö€","a":null,"n":-12}`:        `{"a":null,"n":-12,"ü":"ö€"}`,
		` [ {"z" : 1 , "y" : [ ] } , { } ] `: `[{"y":[],"z":1},{}]`,
	}
	for in, want := range cases {
		got, err := protocol.CanonicalJSON([]byte(in))
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != want {
			t.Errorf("%s: got %s want %s", in, got, want)
		}
	}
	if _, err := protocol.CanonicalJSON([]byte(`{"a":1} x`)); err == nil {
		t.Fatal("trailing data accepted")
	}
	if _, err := protocol.CanonicalJSON([]byte(`{`)); err == nil {
		t.Fatal("broken json accepted")
	}
}

func mustEnv(t *testing.T, typ string, body any) protocol.Envelope {
	t.Helper()
	env, err := protocol.New(typ, body, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	return env
}

// TestProducedMessagesMatchSchemas marshals every message type the node sends.
func TestProducedMessagesMatchSchemas(t *testing.T) {
	hash := strings.Repeat("ab", 32)
	ev := mustEnv(t, protocol.TypeEvent, protocol.Event{Kind: protocol.EventGameDetected, Data: map[string]any{"process": "game.exe"}})
	result := mustEnv(t, protocol.TypeActionResult, protocol.ActionResult{OK: true, Result: map[string]any{"x": 1}})
	result.Re = protocol.NewUUID()
	failed := mustEnv(t, protocol.TypeActionResult, protocol.ActionResult{OK: false, Error: &protocol.Error{Code: protocol.ErrNotAllowed, Message: "no"}})
	failed.Re = protocol.NewUUID()
	ownTrue, ownFalse := true, false
	cases := []struct {
		schema string
		env    protocol.Envelope
	}{
		{"hello", mustEnv(t, protocol.TypeHello, protocol.Hello{NodeID: protocol.NewUUID(), AgentVersion: "0.1.0-dev", ConfigHash: nil})},
		{"hello", mustEnv(t, protocol.TypeHello, protocol.Hello{NodeID: protocol.NewUUID(), AgentVersion: "0.1.0-dev", ConfigHash: &hash})},
		{"inventory", mustEnv(t, protocol.TypeInventory, protocol.Inventory{
			Hardware: protocol.Hardware{CPU: protocol.CPU{Model: "x", Cores: 4}, RAMTotalMB: 1, GPUs: []protocol.GPU{{Index: 0, Name: "g", Vendor: "amd", VRAMTotalMB: 16}}},
			Runner:   protocol.RunnerInfo{Type: "fake", Available: false, Detail: "down"},
			Models:   []protocol.Model{{Name: "m", SizeBytes: 5, Format: "gguf"}, {Name: "n"}},
			Loaded:   []protocol.LoadedModel{{Name: "m", VRAMUsedMB: 3, ContextLength: 8}},
		})},
		{"inventory", mustEnv(t, protocol.TypeInventory, protocol.Inventory{
			Hardware: protocol.Hardware{CPU: protocol.CPU{Model: "x", Cores: 1}, GPUs: []protocol.GPU{}},
			Runner:   protocol.RunnerInfo{Type: "llamaserver"},
			Models:   []protocol.Model{{Name: "own", Own: &ownTrue}, {Name: "shared", Own: &ownFalse}},
			Loaded:   []protocol.LoadedModel{},
		})},
		{"inventory", mustEnv(t, protocol.TypeInventory, protocol.Inventory{
			Hardware: protocol.Hardware{CPU: protocol.CPU{Model: "x", Cores: 1}, GPUs: []protocol.GPU{}},
			Runner:   protocol.RunnerInfo{Type: "llamaserver"},
			Models:   []protocol.Model{},
			Loaded:   []protocol.LoadedModel{},
		})},
		{"heartbeat", mustEnv(t, protocol.TypeHeartbeat, protocol.Heartbeat{State: protocol.StateBusyGaming, GPUs: []protocol.GPUUsage{{Index: 0, VRAMUsedMB: 1}}, Loaded: []protocol.LoadedModel{}})},
		{"action_result", result},
		{"action_result", failed},
		{"event", ev},
		{"queued_events", mustEnv(t, protocol.TypeQueuedEvents, protocol.QueuedEvents{Events: []protocol.Envelope{ev, ev}})},
	}
	for _, c := range cases {
		t.Run(c.schema, func(t *testing.T) {
			if err := schematest.ValidateValue(c.schema, c.env); err != nil {
				raw, _ := json.Marshal(c.env)
				t.Fatalf("%s: %v", raw, err)
			}
			if !protocol.IsUUID(c.env.ID) {
				t.Fatalf("id %q", c.env.ID)
			}
		})
	}
}

func TestUUIDv4(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 1000; i++ {
		u := protocol.NewUUID()
		if !protocol.IsUUID(u) || u[14] != '4' || !strings.ContainsRune("89ab", rune(u[19])) {
			t.Fatalf("bad uuid %s", u)
		}
		if seen[u] {
			t.Fatal("duplicate uuid")
		}
		seen[u] = true
	}
}

func TestParseNodeConfig(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join(schematest.ProtoDir(), "examples", "v1", "valid", "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	var env protocol.Envelope
	if err := json.Unmarshal(raw, &env); err != nil {
		t.Fatal(err)
	}
	var body protocol.ConfigBody
	if err := env.DecodeBody(&body); err != nil {
		t.Fatal(err)
	}
	c, err := protocol.ParseNodeConfig(body.Config)
	if err != nil {
		t.Fatal(err)
	}
	if c.Runner.Type != "llamaserver" || !c.ActionAllowed("models.load") || c.ActionAllowed("models.delete") || c.Watcher.GraceSeconds != 5 || c.IdleTTLSeconds != 900 {
		t.Fatalf("%+v", c)
	}
	bad := []string{
		`{"runner":{"type":"docker"},"heartbeat_interval_seconds":10}`,
		`{"runner":{"type":"fake"},"heartbeat_interval_seconds":301}`,
		`{"runner":{"type":"fake"},"heartbeat_interval_seconds":10,"idle_ttl_seconds":-1}`,
		`{"runner":{"type":"fake"},"heartbeat_interval_seconds":10,"watcher":{"rules":[{"process":" "}]}}`,
		`{"runner":{"type":"fake"},"watcher":{"grace_seconds":-1,"rules":[]}}`,
		`[]`,
	}
	for _, b := range bad {
		if _, err := protocol.ParseNodeConfig([]byte(b)); err == nil {
			t.Errorf("accepted %s", b)
		}
	}
	c, err = protocol.ParseNodeConfig([]byte(`{"runner":{"type":"fake"},"future_field":1}`))
	if err != nil || c.HeartbeatIntervalSeconds != protocol.DefaultHeartbeatIntervalSeconds {
		t.Fatalf("defaults: %+v %v", c, err)
	}
	a := protocol.RunnerConfig{Type: "x", ModelDirs: []string{"a"}}
	if !a.Equal(protocol.RunnerConfig{Type: "x", ModelDirs: []string{"a"}}) || a.Equal(protocol.RunnerConfig{Type: "x", ModelDirs: []string{"b"}}) {
		t.Fatal("RunnerConfig.Equal")
	}
}

func TestAuthorizationHeaderVector(t *testing.T) {
	// The same vector is asserted in the hub tests (hub/tests/test_nodeauth.py).
	key := ed25519.NewKeyFromSeed(bytes.Repeat([]byte{1}, 32))
	got := protocol.AuthorizationHeader(key, "3f0c2a4e-8a51-4b7e-9a0e-1f2d3c4b5a60", "n0nce-for-the-test-vector-0123456789")
	want := "Topcoat-Ed25519 3f0c2a4e-8a51-4b7e-9a0e-1f2d3c4b5a60.n0nce-for-the-test-vector-0123456789.fBJa7nZB5kTdcwIQgOa8rMS3C0lELhcH6wSpWlDt1w-lvNhZXwmqj2Q_eHXUjmkJn7hm_FlZtRBxeZ9JadFFBg"
	if got != want {
		t.Fatalf("got %q\nwant %q", got, want)
	}
}

func TestParseResidentsAndPressure(t *testing.T) {
	raw := []byte(`{"roles":["gpu"],"runner":{"type":"llamaserver"},"allowed_actions":[],"heartbeat_interval_seconds":10,
		"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[],
		"residents":[{"model":"a","priority":80},{"model":"b","auto_reload":false}],
		"pressure":{"sustain_seconds":5,"ram":{"enabled":true}}}`)
	c, err := protocol.ParseNodeConfig(raw)
	if err != nil {
		t.Fatal(err)
	}
	a, ok := c.Resident("a")
	if !ok || a.EffectivePriority() != 80 || !a.Reloads() {
		t.Fatalf("resident a: %+v", a)
	}
	b, _ := c.Resident("b")
	if b.EffectivePriority() != protocol.DefaultResidentPriority || b.Reloads() {
		t.Fatalf("resident b: %+v", b)
	}
	p := c.EffectivePressure()
	if !p.Enabled || p.Sustain != 5*time.Second || !p.RAMEnabled || p.RAMUnloadBelowFreeMB != protocol.DefaultRAMUnloadBelowFreeMB ||
		p.ForeignSharedTriggerMB != protocol.DefaultPressureForeignSharedMB {
		t.Fatalf("pressure: %+v", p)
	}
}

func TestPressureValidation(t *testing.T) {
	base := `{"roles":["gpu"],"runner":{"type":"llamaserver"},"allowed_actions":[],"heartbeat_interval_seconds":10,
		"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[],%s}`
	bad := []string{
		`"residents":[{"model":""}]`,
		`"residents":[{"model":"a"},{"model":"a"}]`,
		`"residents":[{"model":"a","priority":101}]`,
		`"pressure":{"poll_seconds":0}`,
		`"pressure":{"reload_max_foreign_gpu_util":150}`,
		`"pressure":{"ram":{"margin_mb":-1}}`,
		`"pressure":{"foreign_shared_trigger_mb":-1}`,
	}
	for _, b := range bad {
		if _, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, b))); err == nil {
			t.Errorf("accepted %s", b)
		}
	}
}

func TestContextLengthConfig(t *testing.T) {
	base := `{"roles":["gpu"],"runner":{"type":"llamaserver",%s},"allowed_actions":[],"heartbeat_interval_seconds":10,
		"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[],"residents":[{"model":"res","context_length":4096}]}`
	c, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base,
		`"default_context_length":16384,"model_context_lengths":{"big":32768,"res":2048}`)))
	if err != nil {
		t.Fatal(err)
	}
	// resident entry > per-model setting > node default > 0 (runner default)
	for model, want := range map[string]int{"res": 4096, "big": 32768, "other": 16384} {
		if got := c.ContextFor(model); got != want {
			t.Errorf("ContextFor(%q) = %d, want %d", model, got, want)
		}
	}
	plain, err := protocol.ParseNodeConfig([]byte(`{"runner":{"type":"fake"}}`))
	if err != nil || plain.ContextFor("x") != 0 {
		t.Fatalf("no setting must mean the runner default: %v %d", err, plain.ContextFor("x"))
	}
	for _, b := range []string{
		`"default_context_length":100`,
		`"default_context_length":2000000`,
		`"model_context_lengths":{"a":10}`,
		`"model_context_lengths":{" ":4096}`,
	} {
		if _, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, b))); err == nil {
			t.Errorf("accepted %s", b)
		}
	}
}

func TestRAMPressureDefaultDependsOnTheGPU(t *testing.T) {
	// ram.enabled left open: on for a node without a dedicated GPU (models live in system
	// memory), off with one; an explicit value always wins (E2-05, D27).
	parse := func(pressure string) protocol.NodeConfig {
		c, err := protocol.ParseNodeConfig([]byte(`{"runner":{"type":"fake"},"pressure":` + pressure + `}`))
		if err != nil {
			t.Fatal(err)
		}
		return c
	}
	open := parse(`{}`)
	if open.EffectivePressureFor(true).RAMEnabled || !open.EffectivePressureFor(false).RAMEnabled {
		t.Fatal("open default: off with a dedicated GPU, on without")
	}
	if open.EffectivePressure().RAMEnabled {
		t.Fatal("EffectivePressure keeps the dedicated-GPU default")
	}
	off := parse(`{"ram":{"enabled":false}}`)
	if off.EffectivePressureFor(false).RAMEnabled {
		t.Fatal("an explicit false must stay off")
	}
	on := parse(`{"ram":{"enabled":true}}`)
	if !on.EffectivePressureFor(true).RAMEnabled {
		t.Fatal("an explicit true must stay on")
	}
	none, _ := protocol.ParseNodeConfig([]byte(`{"runner":{"type":"fake"}}`))
	if none.EffectivePressureFor(false).RAMEnabled {
		t.Fatal("without a pressure object nothing is watched")
	}
}

func TestModelBackendsAndMemoryGuardConfig(t *testing.T) {
	base := `{"roles":["gpu"],"runner":{"type":"llamaserver",%s},"allowed_actions":[],"heartbeat_interval_seconds":10,
		"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[]}`
	c, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base,
		`"model_backends":{"big":{"backend":"cpu"},"half":{"gpu_layers":20},"none":{"gpu_layers":0}},"memory_guard":{"mode":"warn","reserve_mb":2048}`)))
	if err != nil {
		t.Fatal(err)
	}
	mode, reserve := c.Runner.EffectiveGuard()
	if mode != "warn" || reserve != 2048 || c.Runner.ModelBackends["big"].Backend != "cpu" ||
		*c.Runner.ModelBackends["half"].GPULayers != 20 || *c.Runner.ModelBackends["none"].GPULayers != 0 {
		t.Fatalf("%+v", c.Runner)
	}
	plain, _ := protocol.ParseNodeConfig([]byte(`{"runner":{"type":"fake"}}`))
	if m, r := plain.Runner.EffectiveGuard(); m != "refuse" || r != protocol.AutoGuardReserve {
		t.Fatalf("defaults: %s %d", m, r)
	}
	// a changed backend map or guard makes a new runner (it is handed to the runner at creation)
	other, _ := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, `"model_backends":{"big":{"backend":"vulkan"}}`)))
	if c.Runner.Equal(other.Runner) || !c.Runner.Equal(c.Runner) || plain.Runner.Equal(c.Runner) {
		t.Fatal("Equal must see model_backends and memory_guard")
	}
	for _, b := range []string{
		`"model_backends":{"m":{"backend":"rocm"}}`,
		`"model_backends":{" ":{"backend":"cpu"}}`,
		`"model_backends":{"m":{"gpu_layers":-1}}`,
		`"model_backends":{"m":{"gpu_layers":5000}}`,
		`"memory_guard":{"mode":"maybe"}`,
		`"memory_guard":{"reserve_mb":-5}`,
	} {
		if _, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, b))); err == nil {
			t.Errorf("accepted %s", b)
		}
	}
}

func TestEmptyPerModelMapsAreValid(t *testing.T) {
	// Removing the last entry in the GUI leaves {} behind; that must save (hub 400 before).
	c, err := protocol.ParseNodeConfig([]byte(`{"roles":["gpu"],"runner":{"type":"llamaserver","model_backends":{},"model_context_lengths":{}},
		"allowed_actions":[],"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[]}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Runner.ModelBackends) != 0 || len(c.Runner.ModelContextLengths) != 0 {
		t.Fatalf("%+v", c.Runner)
	}
}

func TestYieldAndPowerDefaultsAndValidation(t *testing.T) {
	base := `{"roles":["gpu"],"runner":{"type":"fake"},"allowed_actions":[],"heartbeat_interval_seconds":10,
		"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[]%s}`
	parse := func(extra string) (protocol.NodeConfig, error) {
		return protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, extra)))
	}
	none, err := parse("")
	if err != nil {
		t.Fatal(err)
	}
	// freezing for local use defaults to on without a dedicated GPU and to off with one
	if y := none.EffectiveYieldFor(false); !y.Enabled || !y.LowPriority || y.Idle != 60*time.Second || y.CPUPercent != protocol.DefaultYieldCPUPercent || protocol.DefaultYieldCPUPercent != 60 ||
		y.Sustain != 5*time.Second || y.Resume != 30*time.Second || !y.PauseOnBattery || y.UnloadAfterPause != 15*time.Minute || y.Poll != 5*time.Second {
		t.Fatalf("defaults %+v", y)
	}
	if none.EffectiveYieldFor(true).Enabled {
		t.Fatal("a dedicated GPU keeps freezing off by default")
	}
	explicit, err := parse(`,"yield":{"enabled":true,"low_priority":false,"idle_seconds":10,"cpu_percent":0,"unload_after_pause_seconds":0,"pause_on_battery":false}`)
	if err != nil {
		t.Fatal(err)
	}
	y := explicit.EffectiveYieldFor(true)
	if !y.Enabled || y.LowPriority || y.Idle != 10*time.Second || y.CPUPercent != 0 || y.UnloadAfterPause != 0 || y.PauseOnBattery {
		t.Fatalf("explicit %+v", y)
	}
	for _, bad := range []string{
		`,"yield":{"poll_seconds":0}`, `,"yield":{"idle_seconds":0}`, `,"yield":{"cpu_percent":101}`,
		`,"yield":{"unload_after_pause_seconds":-1}`, `,"power":{"profile":"turbo"}`, `,"power":{"sleep_after_idle_seconds":-5}`,
	} {
		if _, err := parse(bad); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
	if p := none.EffectivePower(); p.Profile != "balanced" || p.IdleBackoff != 3 || p.HeartbeatFactor != 1 || p.SleepAfterIdle != 0 {
		t.Fatalf("power default %+v", p)
	}
	saver, _ := parse(`,"power":{"profile":"saver","sleep_after_idle_seconds":1800}`)
	if p := saver.EffectivePower(); p.IdleBackoff != 6 || p.HeartbeatFactor != 3 || p.SleepAfterIdle != 30*time.Minute {
		t.Fatalf("saver %+v", p)
	}
	perf, _ := parse(`,"power":{"profile":"performance"}`)
	if p := perf.EffectivePower(); p.IdleBackoff != 1 || p.HeartbeatFactor != 1 {
		t.Fatalf("performance %+v", p)
	}
}

func TestJinjaConfig(t *testing.T) {
	base := `{"roles":["gpu"],"runner":{"type":"llamaserver",%s},"allowed_actions":[],"heartbeat_interval_seconds":10,
		"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[]}`
	c, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, `"jinja":true,"model_jinja":{"old":false}`)))
	if err != nil {
		t.Fatal(err)
	}
	if !c.JinjaFor("new") || c.JinjaFor("old") {
		t.Errorf("per-model setting must win over the node default")
	}
	off, _ := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, `"jinja":false,"model_jinja":{"x":true}`)))
	if off.JinjaFor("other") || !off.JinjaFor("x") {
		t.Errorf("node default false, per-model true")
	}
	plain, _ := protocol.ParseNodeConfig([]byte(`{"runner":{"type":"fake"}}`))
	if !plain.JinjaFor("x") {
		t.Errorf("no setting means --jinja")
	}
	if _, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, `"model_jinja":{" ":true}`))); err == nil {
		t.Error("blank model name accepted")
	}
}

func TestProjectorsConfig(t *testing.T) {
	base := `{"roles":["gpu"],"runner":{"type":"llamaserver",%s},"allowed_actions":[],"heartbeat_interval_seconds":10,
		"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[]}`
	c, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, `"projectors":{"a":"mmproj-F16.gguf","b":"none"}`)))
	if err != nil {
		t.Fatal(err)
	}
	if c.Runner.Projectors["a"] != "mmproj-F16.gguf" || c.Runner.Projectors["b"] != "none" {
		t.Fatalf("%v", c.Runner.Projectors)
	}
	// only a file name in the model's own directory: no path, no other file type
	for _, bad := range []string{`{"a":"../x/mmproj.gguf"}`, `{"a":"/etc/passwd"}`, `{"a":"x.bin"}`, `{"a":""}`, `{" ":"x.gguf"}`} {
		if _, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, `"projectors":`+bad))); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
}

func TestNParallelConfig(t *testing.T) {
	base := `{"roles":["gpu"],"runner":{"type":"llamaserver",%s},"allowed_actions":[],"heartbeat_interval_seconds":10,
		"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[]}`
	c, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, `"n_parallel":2,"model_n_parallel":{"big":1}`)))
	if err != nil {
		t.Fatal(err)
	}
	for model, want := range map[string]int{"big": 1, "other": 2} { // per-model > node default
		if got := c.ParallelFor(model); got != want {
			t.Errorf("ParallelFor(%q) = %d, want %d", model, got, want)
		}
	}
	plain, err := protocol.ParseNodeConfig([]byte(`{"runner":{"type":"fake"}}`))
	if err != nil || plain.ParallelFor("x") != 0 {
		t.Fatalf("no setting must mean the runner default: %v %d", err, plain.ParallelFor("x"))
	}
	for _, b := range []string{`"n_parallel":-1`, `"n_parallel":17`, `"model_n_parallel":{"a":0}`, `"model_n_parallel":{" ":1}`} {
		if _, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, b))); err == nil {
			t.Errorf("accepted %s", b)
		}
	}
}

func TestDocsToolsConfig(t *testing.T) {
	base := `{"roles":["tools"],"runner":{"type":"none"},"allowed_actions":[],"heartbeat_interval_seconds":10,
		"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[],"tools":%s}`
	c, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, `{"docs_roots":["/srv/notes"],"docs_exclude":["private-*"]}`)))
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Tools.DocsRoots) != 1 || c.Tools.DocsExclude[0] != "private-*" {
		t.Fatalf("%+v", c.Tools)
	}
	other, _ := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, `{"docs_roots":["/srv/notes"]}`)))
	if c.Tools.Equal(other.Tools) || !c.Tools.Equal(c.Tools) {
		t.Fatal("a changed docs_exclude must count as a config change")
	}
	for _, bad := range []string{`{"docs_roots":["relative/dir"]}`, `{"docs_exclude":[""]}`, `{"docs_exclude":["["]}`} {
		if _, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, bad))); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
}

func TestCheckTargetsConfig(t *testing.T) {
	base := `{"roles":["tools"],"runner":{"type":"none"},"allowed_actions":[],"heartbeat_interval_seconds":10,
		"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[],"tools":%s}`
	good := `{"net_targets":[{"name":"gateway","host":"10.0.0.1","ping":true,"ports":[53,443]}],"dns_names":["status.example.lan"],"cert_targets":["proxy.example.lan","10.0.0.5:8443","[fe80::1]:443"]}`
	c, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, good)))
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Tools.NetTargets) != 1 || c.Tools.NetTargets[0].Ports[1] != 443 || len(c.Tools.CertTargets) != 3 {
		t.Fatalf("%+v", c.Tools)
	}
	other, _ := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, strings.Replace(good, `"ping":true`, `"ping":false`, 1))))
	if c.Tools.Equal(other.Tools) || !c.Tools.Equal(c.Tools) {
		t.Fatal("a changed target must count as a config change")
	}
	for _, bad := range []string{
		`{"net_targets":[{"name":"a","host":"-oProxyCommand=x"}]}`, `{"net_targets":[{"name":"a","host":"a b"}]}`,
		`{"net_targets":[{"name":"a","host":"h"},{"name":"a","host":"h2"}]}`, `{"net_targets":[{"name":"","host":"h"}]}`,
		`{"net_targets":[{"name":"a","host":"h","ports":[0]}]}`, `{"net_targets":[{"name":"a","host":"h","ports":[70000]}]}`,
		`{"dns_names":["a;b"]}`,
		`{"cert_targets":["host:notaport"]}`, `{"cert_targets":["bad host:443"]}`, `{"cert_targets":["h:0"]}`,
	} {
		if _, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, bad))); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
}

func TestTransferConfig(t *testing.T) {
	parse := func(extra string) (protocol.NodeConfig, error) {
		return protocol.ParseNodeConfig([]byte(`{"roles":["gpu"],"runner":{"type":"fake"},"allowed_actions":["models.send"],` +
			`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,"watcher":{"grace_seconds":0,"rules":[]},"capabilities":[]` + extra + `}`))
	}
	c, err := parse(``)
	if err != nil {
		t.Fatal(err)
	}
	if got := c.EffectiveTransfer(); got.PortMin != 47000 || got.PortMax != 47099 || got.TTL != 600*time.Second {
		t.Fatalf("defaults %+v", got)
	}
	c, err = parse(`,"transfer":{"port_min":50000,"port_max":50010,"ttl_seconds":120}`)
	if err != nil {
		t.Fatal(err)
	}
	if got := c.EffectiveTransfer(); got.PortMin != 50000 || got.PortMax != 50010 || got.TTL != 2*time.Minute {
		t.Fatalf("configured %+v", got)
	}
	c, err = parse(`,"transfer":{"port_min":50000}`) // only the minimum: the default maximum is below it
	if err == nil {
		t.Fatalf("min above the default max accepted: %+v", c.EffectiveTransfer())
	}
	for _, bad := range []string{
		`{"port_min":80}`, `{"port_max":70000}`, `{"port_min":47050,"port_max":47040}`,
		`{"ttl_seconds":5}`, `{"ttl_seconds":90000}`,
	} {
		if _, err := parse(`,"transfer":` + bad); err == nil {
			t.Fatalf("%s accepted", bad)
		}
	}
}

func TestNCPUMoeIsPartOfTheConfigFingerprint(t *testing.T) {
	base := `{"roles":["gpu"],"runner":{"type":"llamaserver","model_backends":{"m":{%s}}},"allowed_actions":["models.list"]}`
	a, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, `"gpu_layers":20`)))
	if err != nil {
		t.Fatal(err)
	}
	b, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, `"gpu_layers":20,"n_cpu_moe":8`)))
	if err != nil {
		t.Fatal(err)
	}
	if *b.Runner.ModelBackends["m"].NCPUMoe != 8 {
		t.Fatalf("%+v", b.Runner.ModelBackends["m"])
	}
	if a.Runner.ModelBackends["m"].NCPUMoe != nil {
		t.Fatalf("%+v", a.Runner.ModelBackends["m"])
	}
}
