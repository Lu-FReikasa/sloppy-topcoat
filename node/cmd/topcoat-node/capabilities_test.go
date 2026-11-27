package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"

	"topcoat/node/internal/buildcap"
	"topcoat/node/internal/hubupdate"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/tools"
	"topcoat/node/internal/voice"
	"topcoat/node/internal/workshop"
)

// Stand-ins that switch every optional capability on; none of them is ever called here.
type (
	anyDocker     struct{ tools.DockerAPI }
	anyDockerW    struct{ tools.DockerWriteAPI }
	anyShell      struct{ tools.ShellAPI }
	anyPowerShell struct{ tools.PowerShellAPI }
	anyBrowser    struct{ workshop.BrowserRunner }
	anyRunner     struct{ workshop.Runner }
	anyListener   struct{ voice.Transcriber }
	anySpeaker    struct{ voice.Speaker }
	anyCaller     struct{ buildcap.Caller }
)

func (anyListener) Models() []string { return nil }
func (anySpeaker) Voices() []string  { return nil }

// everyCapability registers what this binary can offer with every option on: the tool capabilities
// (connectors, web, checks, restarts, shell, Windows), the workshop with its runner and browser,
// voice, the build steps and the hub roll-out.
func everyCapability(t *testing.T) []string {
	t.Helper()
	reg := tools.NewRegistry(tools.Options{
		Docker: anyDocker{}, DockerWrite: anyDockerW{}, RestartContainers: []string{"x"}, RestartUnits: []string{"x"},
		DocsRoots: []string{t.TempDir()}, ReadRoots: []string{t.TempDir()},
		Shell: anyShell{}, Windows: anyPowerShell{},
		Web:    tools.WebOptions{SearxngURL: "http://search.example.lan", Fetch: true},
		Checks: tools.CheckOptions{CertTargets: []string{"hub.example.lan:443"}, DNSNames: []string{"hub.example.lan"}},
		Connectors: &tools.Connectors{
			Kuma:     &tools.KumaConfig{URL: "http://kuma.example.lan", APIKey: "k"},
			Omada:    &tools.OmadaConfig{URL: "http://omada.example.lan", OmadaID: "o", ClientID: "c", ClientSecret: "s"},
			Keycloak: &tools.KeycloakConfig{URL: "http://id.example.lan", Realm: "r", ClientID: "c", ClientSecret: "s"},
		},
	})
	w, err := workshop.New(workshop.Options{
		Dir: t.TempDir(), Browser: anyBrowser{}, Runner: anyRunner{},
		RunnerConfig: protocol.WorkshopRunner{Images: map[string]string{protocol.WorkshopBuildNode: "x"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, cp := range w.Registry() {
		mustAdd(t, reg, cp)
	}
	if err := voice.Register(reg, anyListener{}, anySpeaker{}); err != nil {
		t.Fatal(err)
	}
	if err := buildcap.Register(reg, anyCaller{}); err != nil {
		t.Fatal(err)
	}
	mustAdd(t, reg, &hubupdate.Updater{})
	return reg.Names()
}

func mustAdd(t *testing.T, reg *capability.Registry, cp capability.Capability) {
	t.Helper()
	if err := reg.Register(cp); err != nil {
		t.Fatal(err)
	}
}

// listedCapabilities reads proto/capabilities.json, the one list of capability names (issue #206).
func listedCapabilities(t *testing.T) []string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "proto", "capabilities.json"))
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Capabilities []struct {
			Name string `json:"name"`
		} `json:"capabilities"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	names := make([]string, 0, len(file.Capabilities))
	for _, c := range file.Capabilities {
		names = append(names, c.Name)
	}
	sort.Strings(names)
	return names
}

// Every capability the node registers is listed in proto/capabilities.json, and every listed one is
// registered: a new capability without its entry, or an entry left behind, fails here.
func TestCapabilitiesMatchTheProtoFile(t *testing.T) {
	registered := map[string]bool{}
	for _, n := range everyCapability(t) {
		registered[n] = true
	}
	listed := map[string]bool{}
	for _, n := range listedCapabilities(t) {
		listed[n] = true
		if !registered[n] {
			t.Errorf("%s is listed in proto/capabilities.json but the node registers no such capability", n)
		}
	}
	for n := range registered {
		if !listed[n] {
			t.Errorf("%s is registered by the node but missing from proto/capabilities.json", n)
		}
	}
	if len(registered) < 90 {
		t.Errorf("only %d capabilities registered: an option is no longer switched on here", len(registered))
	}
}
