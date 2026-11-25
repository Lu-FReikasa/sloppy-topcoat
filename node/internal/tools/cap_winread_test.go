package tools

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"regexp"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/providers/capability"
)

type fakePS struct {
	script string
	env    map[string]string
	out    string
	err    error
	calls  int
}

func (f *fakePS) Run(_ context.Context, script string, env map[string]string, _ int) ([]byte, error) {
	f.calls++
	f.script, f.env = script, env
	return []byte(f.out), f.err
}

func winCall(t *testing.T, ps PowerShellAPI, name, in string) (map[string]any, error) {
	t.Helper()
	o := Options{Windows: ps, Now: func() time.Time { return time.Date(2026, 10, 5, 12, 0, 0, 0, time.UTC) }}
	c, ok := NewRegistry(o).Get(name)
	if !ok {
		t.Fatalf("%s is not registered", name)
	}
	raw, err := c.Invoke(context.Background(), json.RawMessage(in))
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	return out, nil
}

func TestWindowsCapabilitiesAreOffByDefault(t *testing.T) {
	r := NewRegistry(Options{})
	for _, w := range winCaps {
		if _, ok := r.Get(w.name); ok {
			t.Errorf("%s is offered without Options.Windows", w.name)
		}
	}
}

func TestWindowsCapabilitiesAreReadOnlyAndNamed(t *testing.T) {
	r := NewRegistry(Options{Windows: &fakePS{}})
	for _, w := range winCaps {
		c, ok := r.Get(w.name)
		if !ok {
			t.Fatalf("%s missing", w.name)
		}
		if !strings.HasPrefix(w.name, "windows.") {
			t.Errorf("%s must be named windows.*", w.name)
		}
		var s map[string]any
		if err := json.Unmarshal(c.InputSchema(), &s); err != nil || s["additionalProperties"] != false {
			t.Errorf("%s: schema must be valid JSON with additionalProperties false (%v)", w.name, err)
		}
		if !capabilityReadOnly(c) {
			t.Errorf("%s must be read-only", w.name)
		}
	}
}

// The scripts are the whole attack surface: only reading verbs may appear in them, and no way to
// run text as code or to write it somewhere.
func TestWindowsScriptsOnlyRead(t *testing.T) {
	verb := regexp.MustCompile(`\b([A-Z][a-z]+)-[A-Z][A-Za-z]+`)
	allowed := map[string]bool{"Get": true, "Select": true, "Where": true, "Sort": true, "ConvertTo": true, "ForEach": true, "Join": true}
	banned := regexp.MustCompile(`(?i)get-wmiobject|get-eventlog|\bwmic\b|invoke-|\biex\b|add-type|-command|start-process|\.exe\b.*\s/(set|change|delete|import|export|restore|duplicatescheme|setactive)|\[io\.file\]|new-object|downloadstring|>>?\s*\$|\bsc\.exe\b|\breg\b|\bnet\b`)
	for _, w := range winCaps {
		s := psPrelude + w.script
		for _, m := range verb.FindAllStringSubmatch(s, -1) {
			if !allowed[m[1]] && m[0] != "Out-String" {
				t.Errorf("%s: %s is not a read-only verb", w.name, m[0])
			}
		}
		if banned.MatchString(s) {
			t.Errorf("%s: script contains a forbidden construct: %s", w.name, banned.FindString(s))
		}
		if strings.Count(s, "{") != strings.Count(s, "}") || strings.Count(s, "(") != strings.Count(s, ")") {
			t.Errorf("%s: unbalanced brackets in the script", w.name)
		}
	}
}

func TestWindowsPowerOnlyAsksPowercfgForReading(t *testing.T) {
	for _, w := range winCaps {
		if w.name != "windows.power" {
			continue
		}
		for _, m := range regexp.MustCompile(`@\('(/[a-z]+)'`).FindAllStringSubmatch(w.script, -1) {
			switch m[1] {
			case "/getactivescheme", "/a", "/query", "/lastwake", "/devicequery":
			default:
				t.Errorf("powercfg switch %s is not on the read-only list", m[1])
			}
		}
	}
}

func TestWindowsParametersReachTheScriptOnlyAsEnvironment(t *testing.T) {
	ps := &fakePS{out: `[]`}
	evil := `System"; Remove-Item C:\x; "`
	if _, err := winCall(t, ps, "windows.events", `{"provider":`+quote(evil)+`}`); err == nil {
		t.Fatal("a provider with quotes and semicolons must be refused")
	}
	if _, err := winCall(t, ps, "windows.events", `{"provider":"Microsoft-Windows-Kernel-Power","log":"Application","level":"error","limit":5,"since":"2h"}`); err != nil {
		t.Fatal(err)
	}
	if ps.env["TC_PROVIDER"] != "Microsoft-Windows-Kernel-Power" || ps.env["TC_LOG"] != "Application" ||
		ps.env["TC_LEVELS"] != "1,2" || ps.env["TC_LIMIT"] != "5" || ps.env["TC_SINCE"] != "2026-10-05T10:00:00Z" {
		t.Errorf("unexpected environment: %v", ps.env)
	}
	if strings.Contains(ps.script, "Kernel-Power") {
		t.Error("a parameter value ended up inside the script text")
	}
}

func quote(s string) string { b, _ := json.Marshal(s); return string(b) }

func TestWindowsInputIsValidated(t *testing.T) {
	ps := &fakePS{out: `[]`}
	bad := map[string]string{
		"windows.events":    `{"log":"Security"}`,
		"windows.events ":   `{"limit":1000}`,
		"windows.events  ":  `{"level":"debug"}`,
		"windows.events   ": `{"since":"when i say"}`,
		"windows.services":  `{"state":"all","name":"x; y"}`,
		"windows.programs":  `{"name_contains":"a*b"}`,
		"windows.processes": `{"sort":"io"}`,
		"windows.system":    `{"anything":1}`,
	}
	for name, in := range bad {
		_, err := winCall(t, ps, strings.TrimSpace(name), in)
		var ie *InputError
		if !errors.As(err, &ie) {
			t.Errorf("%s %s: want an input error, got %v", name, in, err)
		}
	}
	if ps.calls != 0 {
		t.Errorf("PowerShell ran %d times for refused input", ps.calls)
	}
}

func TestWindowsTextSearchPassesOnlyAsEnvironment(t *testing.T) {
	ps := &fakePS{out: `[]`}
	if _, err := winCall(t, ps, "windows.events", `{"contains":"audio","limit":7}`); err != nil {
		t.Fatal(err)
	}
	if ps.env["TC_CONTAINS"] != "audio" || ps.env["TC_LIMIT"] != "7" {
		t.Errorf("contains not passed as environment: %v", ps.env)
	}
	if strings.Contains(ps.script, "audio") {
		t.Error("the search text ended up inside the script text")
	}
	if _, err := winCall(t, ps, "windows.services", `{"display":"Windows Audio"}`); err != nil {
		t.Fatal(err)
	}
	if ps.env["TC_DISPLAY"] != "Windows Audio" || !strings.Contains(ps.script, "-DisplayName") {
		t.Errorf("display name not used: %v", ps.env)
	}
}

func TestAServiceAskedForByNameIsNotHiddenByTheProblemFilter(t *testing.T) {
	// live 2026-11-09: name=Audiosrv answered [] six times, because the default filter only lets
	// stopped automatic services through and a running Audiosrv is not one
	ps := &fakePS{out: `{"state":"all","services":[]}`}
	for in, want := range map[string]string{
		`{"name":"Audiosrv"}`:                   "all",
		`{"display":"*Audio*"}`:                 "all",
		`{}`:                                    "problems",
		`{"name":"Audiosrv","state":"running"}`: "running",
	} {
		if _, err := winCall(t, ps, "windows.services", in); err != nil {
			t.Fatal(err)
		}
		if ps.env["TC_STATE"] != want {
			t.Errorf("%s: state %q, want %q", in, ps.env["TC_STATE"], want)
		}
	}
	if !strings.Contains(ps.script, "state=$env:TC_STATE") {
		t.Error("the answer does not name the state filter it used")
	}
}

func TestAnUnknownWildcardProviderIsNoEvents(t *testing.T) {
	ps := &fakePS{out: `[]`}
	if _, err := winCall(t, ps, "windows.events", `{"provider":"*nothing*"}`); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(ps.script, "NoMatchingProvidersFound") {
		t.Error("a provider pattern without a match must be no events, not a failure")
	}
}

func TestWindowsTextSearchInputIsValidated(t *testing.T) {
	ps := &fakePS{out: `[]`}
	for _, in := range []string{
		`{"contains":"a*b"}`,
		`{"contains":"x; y"}`,
		`{"contains":"` + strings.Repeat("a", 101) + `"}`,
	} {
		if _, err := winCall(t, ps, "windows.events", in); err == nil {
			t.Errorf("windows.events %s: want an input error", in)
		}
	}
	if _, err := winCall(t, ps, "windows.services", `{"display":"Audio$(x)"}`); err == nil {
		t.Error("a display name with $ must be refused")
	}
	if ps.calls != 0 {
		t.Errorf("PowerShell ran %d times for refused input", ps.calls)
	}
}

func TestWindowsResultIsParsedMaskedAndCut(t *testing.T) {
	ps := &fakePS{out: "\n" + `{"os":"Windows 11","token":"x"}` + "\n"}
	out, err := winCall(t, ps, "windows.system", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	d, _ := out["data"].(map[string]any)
	if d["os"] != "Windows 11" || out["truncated"] != false || out["query"] != "windows.system" {
		t.Errorf("unexpected result: %v", out)
	}
	ps.out = `[{"command":"app.exe --password=hunter2hunter2"}]`
	out, _ = winCall(t, ps, "windows.startup", `{}`)
	if b, _ := json.Marshal(out); strings.Contains(string(b), "hunter2") {
		t.Errorf("a secret was not masked: %s", b)
	}
	ps.out = strings.Repeat("a", DefaultMaxOutputBytes+10)
	out, _ = winCall(t, ps, "windows.processes", `{}`)
	if out["truncated"] != true || !strings.HasPrefix(out["text"].(string), strings.Repeat("a", DefaultMaxOutputBytes)) {
		t.Errorf("long output not cut: truncated=%v", out["truncated"])
	}
	ps.out = "not json"
	if out, _ = winCall(t, ps, "windows.processes", `{}`); out["text"] != "not json" {
		t.Errorf("non-JSON output must come back as text: %v", out)
	}
}

func TestWindowsRunnerErrorsPassThrough(t *testing.T) {
	if _, err := winCall(t, &fakePS{err: errors.New("powershell failed")}, "windows.system", `{}`); err == nil {
		t.Fatal("want the runner's error")
	}
}

func TestPowerShellRunnerRefusesOffWindows(t *testing.T) {
	if _, err := (PowerShell{}).Run(context.Background(), "1", nil, 10); err == nil && !isWindows() {
		t.Fatal("the real runner must refuse to run on a non-Windows host")
	}
}

func TestEncodePSIsUTF16LEBase64(t *testing.T) {
	b, err := base64.StdEncoding.DecodeString(encodePS("Aä😀"))
	if err != nil {
		t.Fatal(err)
	}
	want := []byte{'A', 0, 0xE4, 0, 0x3D, 0xD8, 0x00, 0xDE}
	if string(b) != string(want) {
		t.Errorf("got % x, want % x", b, want)
	}
}

func isWindows() bool { return runtime.GOOS == "windows" }

func capabilityReadOnly(c capability.Capability) bool { return capability.InfoOf(c).ReadOnly }

func TestWindowsEventsTreatsAnUnknownProviderAsNoEvents(t *testing.T) {
	ps := &fakePS{out: `[]`}
	if _, err := winCall(t, ps, "windows.events", `{"provider":"audio"}`); err != nil {
		t.Fatal(err)
	}
	// a plain name is checked against the system before the query; the name itself stays out of the script
	if !strings.Contains(ps.script, "-ListProvider") || strings.Contains(ps.script, "audio") {
		t.Errorf("the provider check is missing or the name leaked into the script")
	}
	if ps.env["TC_PROVIDER"] != "audio" {
		t.Errorf("provider not passed as environment: %v", ps.env)
	}
}

// PowerShell never prompts and loads no profile: a prompt would hang until the timeout, a profile
// would run code the scripts did not bring.
func TestPowerShellRunsNonInteractiveWithoutProfile(t *testing.T) {
	args := psArgs("Get-Date")
	for _, want := range []string{"-NoProfile", "-NonInteractive", "-NoLogo"} {
		if !slices.Contains(args, want) {
			t.Errorf("%s missing in %q", want, args)
		}
	}
	if n := len(args); n < 2 || args[n-2] != "-EncodedCommand" || args[n-1] != encodePS(psPrelude+"Get-Date") {
		t.Errorf("the script is not the encoded last argument: %q", args)
	}
}
