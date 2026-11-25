package agent

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/workshop"
)

// readOnlyProbe stands in for a capability that is not part of the workshop.
type readOnlyProbe struct{}

func (readOnlyProbe) Name() string                 { return "docker.ps" }
func (readOnlyProbe) InputSchema() json.RawMessage { return json.RawMessage(`{"type":"object"}`) }
func (readOnlyProbe) Info() capability.Info        { return capability.Info{ReadOnly: true, Description: "x"} }
func (readOnlyProbe) Invoke(context.Context, json.RawMessage) (json.RawMessage, error) {
	return json.RawMessage(`{}`), nil
}

func devSetup(t *testing.T) (*harness, *hubConn, protocol.Inventory) {
	t.Helper()
	cfg := `{"roles":["dev"],"runner":{"type":"none"},"allowed_actions":["capability.invoke"],` +
		`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,"watcher":{"rules":[]},` +
		`"capabilities":["repo.status","repo.write","docker.ps"]}`
	return toolsSetup(t, cfg, func(o *Options) {
		o.NewCapabilities = func(*protocol.ToolsConfig) *capability.Registry {
			reg := capability.NewRegistry()
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
}

func TestTheInventoryMarksWorkshopCapabilitiesAsScratch(t *testing.T) {
	_, _, inv := devSetup(t)
	scratch, readOnly := 0, 0
	for _, c := range inv.Capabilities {
		switch {
		case c.Scratch && c.ReadOnly:
			t.Errorf("%s is both read-only and scratch", c.Name)
		case c.Scratch:
			scratch++
		case c.ReadOnly:
			readOnly++
		}
	}
	if scratch == 0 || readOnly == 0 {
		t.Fatalf("scratch=%d read_only=%d", scratch, readOnly)
	}
}

func TestAScratchCapabilityRunsThroughInvokeWithoutAConfirmation(t *testing.T) {
	_, hc, _ := devSetup(t)
	// repo.write is not read-only, yet capability.invoke runs it: it only touches the scratch area.
	// The repository is not cloned, so it answers with the way forward, not with "needs confirmation".
	res := hc.action(t, "capability.invoke", `{"capability":"repo.write","input":{"repo":"demo","path":"a.txt","content":"x"},`+
		`"repo":{"url":"https://github.com/o/r","branch":"main"}}`)
	if res.OK || res.Error == nil || res.Error.Code != protocol.ErrInvalidArgs || !strings.Contains(res.Error.Message, "repo.sync") {
		t.Fatalf("%+v", res)
	}
}

func TestTheRepositoryKeyIsOnlyForWorkshopCapabilities(t *testing.T) {
	_, hc, _ := devSetup(t)
	res := hc.action(t, "capability.invoke", `{"capability":"docker.ps","input":{},"repo":{"url":"https://github.com/o/r","branch":"main","token":"t"}}`)
	if res.OK || res.Error == nil || res.Error.Code != protocol.ErrInvalidArgs || !strings.Contains(res.Error.Message, "takes no repository") {
		t.Fatalf("%+v", res)
	}
	res = hc.action(t, "capability.invoke", `{"capability":"repo.status","input":{"repo":"demo"},"repo":"nonsense"}`)
	if res.OK || res.Error == nil || res.Error.Code != protocol.ErrInvalidArgs {
		t.Fatalf("%+v", res)
	}
}

func TestTheRepositoryTokenIsNotLogged(t *testing.T) {
	h, hc, _ := devSetup(t)
	hc.action(t, "capability.invoke", `{"capability":"repo.sync","input":{"repo":"demo"},`+
		`"repo":{"url":"https://github.com/o/r","branch":"main","token":"ghp_THISMUSTNOTAPPEAR0123456789abcdef"}}`)
	hc.action(t, "capability.invoke", `{"capability":"repo.status","input":{"repo":"demo"},`+
		`"repo":{"url":"https://github.com/o/r","branch":"main","token":"ghp_THISMUSTNOTAPPEAR0123456789abcdef"}}`)
	if strings.Contains(h.logs.String(), "THISMUSTNOTAPPEAR") {
		t.Fatalf("the token reached the log:\n%s", h.logs.String())
	}
}
