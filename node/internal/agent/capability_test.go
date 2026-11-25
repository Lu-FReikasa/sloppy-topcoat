package agent

import (
	"context"
	"encoding/json"
	"slices"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/providers/fake"
	"topcoat/node/internal/tools"
)

func toolsConfig(caps string, actions string) string {
	return `{"roles":["tools"],"runner":{"type":"none"},"allowed_actions":[` + actions + `],` +
		`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[` + caps + `]}`
}

// toolsSetup starts a node with the fake capability registry and returns its inventory too.
func toolsSetup(t *testing.T, cfg string, mods ...func(*Options)) (*harness, *hubConn, protocol.Inventory) {
	t.Helper()
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), append([]func(*Options){func(o *Options) {
		o.NewCapabilities = func(*protocol.ToolsConfig) *capability.Registry { return fake.NewToolsRegistry() }
	}}, mods...)...)
	h.start(t)
	hc := hub.accept(t)
	if env := hc.next(t); env.Type != protocol.TypeHello {
		t.Fatalf("first message %s", env.Type)
	}
	hc.send(t, protocol.TypeConfig, configBody(t, cfg))
	env := hc.next(t)
	if env.Type != protocol.TypeInventory {
		t.Fatalf("got %s, want inventory", env.Type)
	}
	var inv protocol.Inventory
	if err := env.DecodeBody(&inv); err != nil {
		t.Fatal(err)
	}
	hc.waitType(t, protocol.TypeHeartbeat)
	return h, hc, inv
}

func invokeCap(t *testing.T, hc *hubConn, name, input string) protocol.ActionResult {
	t.Helper()
	return hc.action(t, "capability.invoke", `{"capability":"`+name+`","input":`+input+`}`)
}

func inventoryNames(inv protocol.Inventory) map[string]bool {
	names := map[string]bool{}
	for _, c := range inv.Capabilities {
		names[c.Name] = true
	}
	return names
}

// baseCapabilities is what every node offers whatever its config says: the read capabilities of
// E2-04 plus system.memory and compose.logs (E3-11), the collecting health.check and logs.search,
// and inventory.snapshot. None of them needs a released directory, a target or a connectors file.
var baseCapabilities = []string{
	"system.info", "system.memory", "disk.usage",
	"docker.ps", "docker.inspect", "docker.logs",
	"compose.projects", "compose.file", "compose.logs",
	"journal.logs", "service.status", "service.failed",
	"health.check", "logs.search",
	"inventory.snapshot",
}

// wantCapabilities fails the test unless the inventory offers exactly the base set plus extra:
// a capability that appears without its precondition is as much a failure as one that is missing.
func wantCapabilities(t *testing.T, inv protocol.Inventory, extra ...string) {
	t.Helper()
	want := slices.Concat(baseCapabilities, extra)
	slices.Sort(want)
	var got []string
	for _, c := range inv.Capabilities {
		got = append(got, c.Name)
	}
	slices.Sort(got)
	if !slices.Equal(got, want) {
		var missing, unexpected []string
		for _, n := range want {
			if !slices.Contains(got, n) {
				missing = append(missing, n)
			}
		}
		for _, n := range got {
			if !slices.Contains(want, n) {
				unexpected = append(unexpected, n)
			}
		}
		t.Fatalf("inventory differs: missing %v, unexpected %v (got %v)", missing, unexpected, got)
	}
}

func TestTheInventoryListsWhatTheNodeOffers(t *testing.T) {
	_, _, inv := toolsSetup(t, toolsConfig(`"docker.ps"`, `"capability.invoke"`))
	// on top of the base set the fake node has a fake Uptime Kuma (E2-04c), releases a notes
	// folder (tools.docs_roots, E2-03), has a fake SearXNG and may fetch pages (web.search /
	// web.images, web.fetch / web.image) and watches one gateway and one name (net.check)
	wantCapabilities(t, inv, "kuma.monitors", "docs.list", "docs.read",
		"web.search", "web.images", "web.fetch", "web.image", "net.check")
	for _, c := range inv.Capabilities {
		if !c.ReadOnly || c.Description == "" || !strings.Contains(string(c.InputSchema), `"type":"object"`) {
			t.Errorf("%+v", c)
		}
	}
	if inv.Runner.Type != "fake" && inv.Runner.Type != "none" {
		t.Fatalf("runner %+v", inv.Runner)
	}
}

// Without a released directory (tools.docs_roots, the default) and without a connectors file a
// node offers only the base set: no docs.*, no files.*, no web.*, no connector capability.
func TestTheInventoryOffersNoKnowledgeFilesWithoutAReleasedDirectory(t *testing.T) {
	_, _, inv := toolsSetup(t, toolsConfig(`"docker.ps"`, `"capability.invoke"`), func(o *Options) {
		o.NewCapabilities = func(*protocol.ToolsConfig) *capability.Registry {
			return tools.NewRegistry(tools.Options{})
		}
	})
	wantCapabilities(t, inv)
}

// With docs_roots the node offers docs.list and docs.read, and only those two are added.
func TestTheInventoryOffersKnowledgeFilesWithAReleasedDirectory(t *testing.T) {
	dir := t.TempDir()
	_, _, inv := toolsSetup(t, toolsConfig(`"docs.list","docs.read"`, `"capability.invoke"`), func(o *Options) {
		o.NewCapabilities = func(*protocol.ToolsConfig) *capability.Registry {
			return tools.NewRegistry(tools.Options{DocsRoots: []string{dir}})
		}
	})
	wantCapabilities(t, inv, "docs.list", "docs.read")
}

func TestACapabilityRunsOnlyWhenTheNodeSwitchedItOn(t *testing.T) {
	_, hc, _ := toolsSetup(t, toolsConfig(`"docker.ps","disk.usage"`, `"capability.invoke"`))
	res := invokeCap(t, hc, "docker.ps", `{}`)
	if !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	var body struct {
		Capability string
		Output     struct{ Containers []map[string]any }
		Bytes      int
		DurationMS int64 `json:"duration_ms"`
	}
	raw, _ := json.Marshal(res.Result)
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatal(err)
	}
	if body.Capability != "docker.ps" || len(body.Output.Containers) != 3 || body.Bytes == 0 {
		t.Fatalf("%s", raw)
	}
	// offered by the build, but not switched on in the config
	if res := invokeCap(t, hc, "journal.logs", `{"unit":"app.service"}`); res.OK || res.Error.Code != protocol.ErrNotAllowed {
		t.Fatalf("%+v", res)
	}
	if res := invokeCap(t, hc, "shell.exec", `{"cmd":"id"}`); res.OK || res.Error.Code != protocol.ErrNotAllowed {
		t.Fatalf("%+v", res)
	}
}

func TestCapabilityInvokeNeedsTheActionPermissionToo(t *testing.T) {
	_, hc, _ := toolsSetup(t, toolsConfig(`"docker.ps"`, `"models.list"`))
	if res := invokeCap(t, hc, "docker.ps", `{}`); res.OK || res.Error.Code != protocol.ErrNotAllowed {
		t.Fatalf("%+v", res)
	}
}

func TestBadInputAndFailuresHaveTheirOwnCodes(t *testing.T) {
	_, hc, _ := toolsSetup(t, toolsConfig(`"docker.logs","service.status","system.info"`, `"capability.invoke"`))
	if res := invokeCap(t, hc, "docker.logs", `{"container":"../etc"}`); res.OK || res.Error.Code != protocol.ErrInvalidArgs {
		t.Fatalf("%+v", res)
	}
	if res := hc.action(t, "capability.invoke", `{"input":{}}`); res.OK || res.Error.Code != protocol.ErrInvalidArgs {
		t.Fatalf("missing capability: %+v", res)
	}
	if res := hc.action(t, "capability.invoke", `{"capability":"system.info","input":{},"sudo":true}`); res.OK || res.Error.Code != protocol.ErrInvalidArgs {
		t.Fatalf("unknown arg: %+v", res)
	}
	// the secret in the fake container's log never leaves the node
	res := invokeCap(t, hc, "docker.logs", `{"container":"site-worker-1"}`)
	if !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	raw, _ := json.Marshal(res.Result)
	if strings.Contains(string(raw), "demo-password") || !strings.Contains(string(raw), "masked:url_credentials") {
		t.Fatalf("%s", raw)
	}
}

type slowCap struct{ block chan struct{} }

func (slowCap) Name() string                 { return "test.slow" }
func (slowCap) InputSchema() json.RawMessage { return json.RawMessage(`{"type":"object"}`) }
func (slowCap) Info() capability.Info        { return capability.Info{Description: "blocks", ReadOnly: true} }
func (s slowCap) Invoke(ctx context.Context, _ json.RawMessage) (json.RawMessage, error) {
	select {
	case <-s.block:
		return json.RawMessage(`{}`), nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

type mutatingCap struct{}

func (mutatingCap) Name() string                 { return "test.restart" }
func (mutatingCap) InputSchema() json.RawMessage { return json.RawMessage(`{"type":"object"}`) }
func (mutatingCap) Invoke(context.Context, json.RawMessage) (json.RawMessage, error) {
	panic("a capability that is not read-only must never run through capability.invoke")
}

func TestCallsAreTimeLimitedBoundedInNumberAndReadOnly(t *testing.T) {
	block := make(chan struct{})
	reg := capability.NewRegistry()
	for _, c := range []capability.Capability{slowCap{block}, mutatingCap{}} {
		if err := reg.Register(c); err != nil {
			t.Fatal(err)
		}
	}
	cfg := `{"roles":["tools"],"runner":{"type":"none"},"allowed_actions":["capability.invoke"],` +
		`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,"watcher":{"rules":[]},"tools":{"timeout_seconds":1},"capabilities":["test.slow","test.restart"]}`
	_, hc, _ := toolsSetup(t, cfg, func(o *Options) {
		o.NewCapabilities = func(*protocol.ToolsConfig) *capability.Registry { return reg }
	})
	// not read-only: refused, never run
	if res := invokeCap(t, hc, "test.restart", `{}`); res.OK || res.Error.Code != protocol.ErrNotAllowed || !strings.Contains(res.Error.Message, "confirmation") {
		t.Fatalf("%+v", res)
	}
	// time limit
	start := time.Now()
	if res := invokeCap(t, hc, "test.slow", `{}`); res.OK || res.Error.Code != protocol.ErrTimeout {
		t.Fatalf("%+v", res)
	}
	if d := time.Since(start); d > 5*time.Second {
		t.Fatalf("took %s", d)
	}
	close(block)
}

// A web input with private data is refused with its own code, and neither the node's log nor the
// answer holds what was refused (E3-04).
func TestAWebInputWithPrivateDataIsRefusedWithItsOwnCodeAndNotLogged(t *testing.T) {
	h, hc, _ := toolsSetup(t, toolsConfig(`"web.search","web.fetch"`, `"capability.invoke"`))
	for name, input := range map[string]string{
		"web.search": `{"query":"password of alpha 10.77.0.3 hunter2hunter2"}`,
		"web.fetch":  `{"url":"http://10.77.0.3/admin?token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"}`,
	} {
		res := invokeCap(t, hc, name, input)
		if res.OK || res.Error.Code != protocol.ErrPrivateData {
			t.Fatalf("%s: %+v", name, res)
		}
		if strings.Contains(res.Error.Message, "10.77.0.3") || strings.Contains(res.Error.Message, "hunter2") {
			t.Fatalf("the refusal repeats the value: %s", res.Error.Message)
		}
	}
	if res := invokeCap(t, hc, "web.search", `{"query":"llama.cpp kv cache size"}`); !res.OK {
		t.Fatalf("a public query must pass: %+v", res)
	}
	logs := h.logs.String()
	for _, secret := range []string{"10.77.0.3", "hunter2hunter2", "ghp_abcdefghijklmnopqrstuvwxyz"} {
		if strings.Contains(logs, secret) {
			t.Fatalf("the node log holds %q", secret)
		}
	}
	if !strings.Contains(logs, "refused a web input with private data") {
		t.Fatalf("the refusal is not in the log:\n%s", logs)
	}
}

// restartCap is a state-changing capability for the tests of capability.execute.
type restartCap struct{ ran *int }

func (restartCap) Name() string                 { return "test.restart" }
func (restartCap) InputSchema() json.RawMessage { return json.RawMessage(`{"type":"object"}`) }
func (restartCap) Info() capability.Info {
	return capability.Info{Description: "restarts", ReadOnly: false}
}
func (r restartCap) Invoke(context.Context, json.RawMessage) (json.RawMessage, error) {
	*r.ran++
	return json.RawMessage(`{"restarted":true}`), nil
}

const (
	testProposal = "0b9f3a42-7c1d-4e55-9a10-3f6c2d8e1b77"
	testHash     = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
)

func executeSetup(t *testing.T, actions string, ran *int) *hubConn {
	t.Helper()
	reg := capability.NewRegistry()
	_ = reg.Register(restartCap{ran})
	_ = reg.Register(slowCap{make(chan struct{})})
	cfg := `{"roles":["tools"],"runner":{"type":"none"},"allowed_actions":[` + actions + `],` +
		`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":["test.restart","test.slow"]}`
	_, hc, _ := toolsSetup(t, cfg, func(o *Options) {
		o.NewCapabilities = func(*protocol.ToolsConfig) *capability.Registry { return reg }
	})
	return hc
}

func TestACapabilityThatChangesStateRunsOnlyThroughExecuteWithAConfirmedProposal(t *testing.T) {
	ran := 0
	hc := executeSetup(t, `"capability.invoke","capability.execute"`, &ran)
	exec := func(extra string) protocol.ActionResult {
		return hc.action(t, "capability.execute", `{"capability":"test.restart","input":{}`+extra+`}`)
	}
	// invoke never runs it
	if res := invokeCap(t, hc, "test.restart", `{}`); res.OK || ran != 0 {
		t.Fatalf("invoke ran a state-changing capability: %+v", res)
	}
	// execute without the proposal's id and hash, or with malformed ones, does not run
	for _, extra := range []string{``, `,"proposal":"` + testProposal + `"`, `,"hash":"` + testHash + `"`,
		`,"proposal":"nope","hash":"` + testHash + `"`, `,"proposal":"` + testProposal + `","hash":"abc"`} {
		if res := exec(extra); res.OK || res.Error.Code != protocol.ErrInvalidArgs || ran != 0 {
			t.Fatalf("%q: ran=%d %+v", extra, ran, res)
		}
	}
	// no extra argument can be smuggled in
	if res := exec(`,"proposal":"` + testProposal + `","hash":"` + testHash + `","sudo":true`); res.OK || ran != 0 {
		t.Fatalf("%+v", res)
	}
	// a read-only capability is not run by execute
	if res := hc.action(t, "capability.execute", `{"capability":"test.slow","input":{},"proposal":"`+testProposal+`","hash":"`+testHash+`"}`); res.OK || res.Error.Code != protocol.ErrNotAllowed {
		t.Fatalf("%+v", res)
	}
	// the confirmed one runs, once
	res := exec(`,"proposal":"` + testProposal + `","hash":"` + testHash + `"`)
	if !res.OK || ran != 1 {
		t.Fatalf("ran=%d %+v", ran, res)
	}
}

func TestExecuteIsRefusedWhereTheConfigDoesNotAllowIt(t *testing.T) {
	ran := 0
	hc := executeSetup(t, `"capability.invoke"`, &ran)
	res := hc.action(t, "capability.execute", `{"capability":"test.restart","input":{},"proposal":"`+testProposal+`","hash":"`+testHash+`"}`)
	if res.OK || res.Error.Code != protocol.ErrNotAllowed || ran != 0 {
		t.Fatalf("ran=%d %+v", ran, res)
	}
}

// A tools config that changes while the session runs changes what the node offers; the hub must
// hear about it without a service restart (found on real hardware: docker.restart only showed up
// after the node service was restarted).
func TestAChangedToolsConfigIsFollowedByANewInventory(t *testing.T) {
	_, hc, inv := toolsSetup(t, toolsConfig(`"docker.ps"`, `"capability.invoke"`), func(o *Options) {
		o.NewCapabilities = func(c *protocol.ToolsConfig) *capability.Registry {
			reg := fake.NewToolsRegistry()
			if c != nil && len(c.RestartContainers) > 0 {
				_ = reg.Register(restartCap{new(int)})
			}
			return reg
		}
	})
	if inventoryNames(inv)["test.restart"] {
		t.Fatal("offered before it was configured")
	}
	cfg := strings.Replace(toolsConfig(`"docker.ps"`, `"capability.invoke"`), `"capabilities":`, `"tools":{"restart_containers":["web"]},"capabilities":`, 1)
	hc.send(t, protocol.TypeConfig, configBody(t, cfg))
	env := hc.waitType(t, protocol.TypeInventory)
	var got protocol.Inventory
	if err := env.DecodeBody(&got); err != nil {
		t.Fatal(err)
	}
	if !inventoryNames(got)["test.restart"] {
		t.Fatalf("the new capability is not in the inventory that followed the config: %v", inventoryNames(got))
	}
}

// manageCap is a management capability of the workshop (D76) for the tests of workshop.manage.
type manageCap struct{ ran *int }

func (manageCap) Name() string                 { return "workshop.create" }
func (manageCap) InputSchema() json.RawMessage { return json.RawMessage(`{"type":"object"}`) }
func (manageCap) Info() capability.Info {
	return capability.Info{Description: "creates", Manage: true}
}
func (m manageCap) Invoke(context.Context, json.RawMessage) (json.RawMessage, error) {
	*m.ran++
	return json.RawMessage(`{"created":true}`), nil
}

func TestTheWorkshopsManagementStepsRunOnlyThroughWorkshopManage(t *testing.T) {
	ran, managed := 0, 0
	reg := capability.NewRegistry()
	_ = reg.Register(restartCap{&ran})
	_ = reg.Register(manageCap{&managed})
	cfg := `{"roles":["dev"],"runner":{"type":"none"},"allowed_actions":["capability.invoke","capability.execute","workshop.manage"],` +
		`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":["test.restart","workshop.create"]}`
	_, hc, _ := toolsSetup(t, cfg, func(o *Options) {
		o.NewCapabilities = func(*protocol.ToolsConfig) *capability.Registry { return reg }
	})
	// an assistant's calls never reach a management step: neither invoke nor a confirmed execute
	if res := invokeCap(t, hc, "workshop.create", `{}`); res.OK || res.Error.Code != protocol.ErrNotAllowed {
		t.Fatalf("invoke: %+v", res)
	}
	if res := hc.action(t, "capability.execute", `{"capability":"workshop.create","input":{},"proposal":"`+testProposal+`","hash":"`+testHash+`"}`); res.OK || managed != 0 {
		t.Fatalf("execute: %+v", res)
	}
	// workshop.manage runs it, and nothing else
	if res := hc.action(t, "workshop.manage", `{"capability":"test.restart","input":{}}`); res.OK || ran != 0 {
		t.Fatalf("manage ran another capability: %+v", res)
	}
	if res := hc.action(t, "workshop.manage", `{"capability":"workshop.create","input":{},"sudo":true}`); res.OK || managed != 0 {
		t.Fatalf("an extra argument: %+v", res)
	}
	if res := hc.action(t, "workshop.manage", `{"capability":"workshop.create","input":{}}`); !res.OK || managed != 1 {
		t.Fatalf("managed=%d %+v", managed, res)
	}
}

func TestWorkshopManageNeedsTheActionPermission(t *testing.T) {
	managed := 0
	reg := capability.NewRegistry()
	_ = reg.Register(manageCap{&managed})
	cfg := `{"roles":["dev"],"runner":{"type":"none"},"allowed_actions":["capability.invoke"],` +
		`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":["workshop.create"]}`
	_, hc, _ := toolsSetup(t, cfg, func(o *Options) {
		o.NewCapabilities = func(*protocol.ToolsConfig) *capability.Registry { return reg }
	})
	if res := hc.action(t, "workshop.manage", `{"capability":"workshop.create","input":{}}`); res.OK || res.Error.Code != protocol.ErrNotAllowed || managed != 0 {
		t.Fatalf("%+v", res)
	}
}
