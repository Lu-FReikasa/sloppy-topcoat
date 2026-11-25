package agent

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"topcoat/node/internal/buildcap"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/workshop"
)

type fakeBus struct {
	calls []string
}

func (f *fakeBus) Call(_ context.Context, service, action string, input json.RawMessage) (json.RawMessage, error) {
	f.calls = append(f.calls, service+" "+action+" "+string(input))
	return json.RawMessage(`{"commit":"` + strings.Repeat("a", 40) + `"}`), nil
}

func buildSetup(t *testing.T, enabled string) (*hubConn, protocol.Inventory, *fakeBus) {
	t.Helper()
	bus := &fakeBus{}
	cfg := `{"roles":["build"],"runner":{"type":"none"},"allowed_actions":["build.manage"],` +
		`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,"watcher":{"rules":[]},` +
		`"capabilities":[` + enabled + `]}`
	_, hc, inv := toolsSetup(t, cfg, func(o *Options) {
		o.NewCapabilities = func(*protocol.ToolsConfig) *capability.Registry {
			reg := capability.NewRegistry()
			if err := buildcap.Register(reg, bus); err != nil {
				t.Fatal(err)
			}
			w, err := workshop.New(workshop.Options{Dir: t.TempDir()})
			if err != nil {
				t.Fatal(err)
			}
			for _, c := range w.Registry() {
				_ = reg.Register(c)
			}
			_ = reg.Register(readOnlyProbe{})
			return reg
		}
	})
	return hc, inv, bus
}

const enabled = `"build.fetch","build.node_release","build.hub_image","build.smoke_test","docker.ps","repo.status","workshop.create"`

func TestTheInventoryMarksTheBuildStepsAsTheAdminsOwn(t *testing.T) {
	_, inv, _ := buildSetup(t, enabled)
	found := 0
	for _, c := range inv.Capabilities {
		if strings.HasPrefix(c.Name, "build.") {
			found++
			if !c.Manage || c.ReadOnly || c.Scratch {
				t.Errorf("%s: manage=%v read_only=%v scratch=%v", c.Name, c.Manage, c.ReadOnly, c.Scratch)
			}
		}
	}
	if found != 5 {
		t.Fatalf("found %d build steps", found)
	}
}

func TestBuildManageRunsABuildStepThroughTheBus(t *testing.T) {
	hc, _, bus := buildSetup(t, enabled)
	res := hc.action(t, "build.manage", `{"capability":"build.fetch","input":{"ref":"main","source":"release_repo"}}`)
	if !res.OK {
		t.Fatalf("%+v", res)
	}
	if len(bus.calls) != 1 || bus.calls[0] != `build build.fetch {"ref":"main","source":"release_repo"}` {
		t.Fatalf("calls: %v", bus.calls)
	}
}

func TestOnlyBuildManageRunsABuildStep(t *testing.T) {
	hc, _, bus := buildSetup(t, enabled)
	for _, action := range []string{"capability.invoke", "capability.execute", "workshop.manage"} {
		body := `{"capability":"build.fetch","input":{"ref":"main","source":"release_repo"}}`
		if action == "capability.execute" {
			body = `{"capability":"build.fetch","input":{"ref":"main","source":"release_repo"},"proposal":"11111111-2222-4333-8444-555555555555","hash":"` + strings.Repeat("a", 64) + `"}`
		}
		res := hc.action(t, action, body)
		if res.OK || res.Error == nil || res.Error.Code != protocol.ErrNotAllowed {
			t.Errorf("%s ran a build step: %+v", action, res)
		}
	}
	if len(bus.calls) != 0 {
		t.Fatalf("the bus was reached: %v", bus.calls)
	}
}

func TestBuildManageRunsNothingButBuildSteps(t *testing.T) {
	hc, _, bus := buildSetup(t, enabled)
	for _, body := range []string{
		`{"capability":"docker.ps","input":{}}`,
		`{"capability":"repo.status","input":{"repo":"demo"}}`,
		`{"capability":"workshop.create","input":{"repo":"demo"}}`,
		`{"capability":"build.unknown","input":{}}`,
	} {
		res := hc.action(t, "build.manage", body)
		if res.OK || res.Error == nil || res.Error.Code != protocol.ErrNotAllowed {
			t.Errorf("%s: %+v", body, res)
		}
	}
	if len(bus.calls) != 0 {
		t.Fatalf("the bus was reached: %v", bus.calls)
	}
}

func TestABuildStepThatTheConfigDoesNotSwitchOnDoesNotRun(t *testing.T) {
	hc, _, bus := buildSetup(t, `"build.fetch"`)
	res := hc.action(t, "build.manage", `{"capability":"build.hub_image","input":{"commit":"`+strings.Repeat("a", 40)+`","build_id":"11111111-2222-4333-8444-555555555555"}}`)
	if res.OK || res.Error == nil || res.Error.Code != protocol.ErrNotAllowed {
		t.Fatalf("%+v", res)
	}
	if len(bus.calls) != 0 {
		t.Fatalf("the bus was reached: %v", bus.calls)
	}
}

func TestTheInputOfABuildStepIsCheckedByItsSchemaBeforeTheBus(t *testing.T) {
	hc, _, bus := buildSetup(t, enabled)
	for _, body := range []string{
		`{"capability":"build.fetch","input":{"ref":"main; rm -rf /","source":"release_repo"}}`,
		`{"capability":"build.fetch","input":{"ref":"main","source":"release_repo","cmd":"ls"}}`,
		`{"capability":"build.node_release","input":{"commit":"main","version":"0.3.0","build_id":"11111111-2222-4333-8444-555555555555"}}`,
		`{"capability":"build.node_release","input":{"commit":"` + strings.Repeat("a", 40) + `","version":"latest","build_id":"11111111-2222-4333-8444-555555555555"}}`,
		`{"capability":"build.smoke_test","input":{"image":"--privileged"}}`,
	} {
		res := hc.action(t, "build.manage", body)
		if res.OK {
			t.Errorf("%s was accepted", body)
		}
	}
	if len(bus.calls) != 0 {
		t.Fatalf("the bus was reached: %v", bus.calls)
	}
}

func TestBuildManageTakesNoRepositoryAndNoOtherKeys(t *testing.T) {
	hc, _, _ := buildSetup(t, enabled)
	res := hc.action(t, "build.manage", `{"capability":"build.fetch","input":{"ref":"main","source":"release_repo"},"repo":{"url":"https://x","branch":"main"}}`)
	if res.OK || res.Error == nil || res.Error.Code != protocol.ErrInvalidArgs {
		t.Fatalf("%+v", res)
	}
}

// --- hub.manage (NS-01, phase 5) ---------------------------------------------------------------

type fakeUpdater struct{ calls []string }

func (f *fakeUpdater) Name() string                 { return "hub.update" }
func (f *fakeUpdater) InputSchema() json.RawMessage { return json.RawMessage(`{"type":"object"}`) }
func (f *fakeUpdater) Info() capability.Info        { return capability.Info{Manage: true, Description: "x"} }
func (f *fakeUpdater) Invoke(_ context.Context, in json.RawMessage) (json.RawMessage, error) {
	f.calls = append(f.calls, string(in))
	return json.RawMessage(`{"ok":true}`), nil
}

func hubSetup(t *testing.T) (*hubConn, *fakeUpdater, *fakeBus) {
	t.Helper()
	up, bus := &fakeUpdater{}, &fakeBus{}
	cfg := `{"roles":["tools"],"runner":{"type":"none"},"allowed_actions":["hub.manage","build.manage"],` +
		`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,"watcher":{"rules":[]},` +
		`"capabilities":["hub.update","build.fetch","docker.ps"]}`
	_, hc, _ := toolsSetup(t, cfg, func(o *Options) {
		o.NewCapabilities = func(*protocol.ToolsConfig) *capability.Registry {
			reg := capability.NewRegistry()
			_ = reg.Register(up)
			_ = buildcap.Register(reg, bus)
			_ = reg.Register(readOnlyProbe{})
			return reg
		}
	})
	return hc, up, bus
}

func TestHubManageRunsTheRollOutAndNothingElseDoes(t *testing.T) {
	hc, up, _ := hubSetup(t)
	res := hc.action(t, "hub.manage", `{"capability":"hub.update","input":{"image":"x:y"}}`)
	if !res.OK || len(up.calls) != 1 {
		t.Fatalf("%+v calls=%v", res, up.calls)
	}
	for _, action := range []string{"capability.invoke", "workshop.manage", "build.manage"} {
		res := hc.action(t, action, `{"capability":"hub.update","input":{"image":"x:y"}}`)
		if res.OK || res.Error == nil || res.Error.Code != protocol.ErrNotAllowed {
			t.Errorf("%s ran the roll-out: %+v", action, res)
		}
	}
	if len(up.calls) != 1 {
		t.Fatalf("calls: %v", up.calls)
	}
}

func TestHubManageRunsNoOtherCapability(t *testing.T) {
	hc, up, bus := hubSetup(t)
	for _, c := range []string{"build.fetch", "docker.ps", "hub.other"} {
		res := hc.action(t, "hub.manage", `{"capability":"`+c+`","input":{}}`)
		if res.OK || res.Error == nil || res.Error.Code != protocol.ErrNotAllowed {
			t.Errorf("%s: %+v", c, res)
		}
	}
	if len(up.calls) != 0 || len(bus.calls) != 0 {
		t.Fatalf("something ran: %v %v", up.calls, bus.calls)
	}
}
