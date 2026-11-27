package tools

import (
	"context"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode/utf8"

	"topcoat/node/internal/providers/capability"
)

// --- the file -------------------------------------------------------------------------------

const goodFile = `{
  "uptime_kuma": {"url": "http://10.0.0.5:3001", "api_key": "uk1_abcdef_secretsecretsecret"},
  "omada": {"url": "https://10.0.0.6:8043", "omada_id": "oid123", "client_id": "cid-12345", "client_secret": "csecret-67890", "site": "Home"}
}`

func writeConnectors(t *testing.T, body string, mode os.FileMode) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "connectors.json")
	if err := os.WriteFile(p, []byte(body), mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(p, mode); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestConnectorsFileMustBePrivate(t *testing.T) {
	if _, err := LoadConnectors(filepath.Join(t.TempDir(), "nope.json")); !errors.Is(err, ErrNoConnectorsFile) {
		t.Fatalf("a missing file is not an error of its own: %v", err)
	}
	c, err := LoadConnectors(writeConnectors(t, goodFile, 0o600))
	if err != nil || c.Kuma == nil || c.Omada == nil || c.Omada.Site != "Home" {
		t.Fatalf("%v %+v", err, c)
	}
	if runtime.GOOS == "windows" {
		return // the permission bits mean nothing there (see connectors_windows.go)
	}
	for _, mode := range []os.FileMode{0o644, 0o640, 0o604, 0o660} {
		if _, err := LoadConnectors(writeConnectors(t, goodFile, mode)); err == nil || !strings.Contains(err.Error(), "readable by others") {
			t.Errorf("mode %o accepted: %v", mode, err)
		}
	}
	// a link to a good file is refused too
	dir := t.TempDir()
	real := writeConnectors(t, goodFile, 0o600)
	link := filepath.Join(dir, "link.json")
	if err := os.Symlink(real, link); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadConnectors(link); err == nil || !strings.Contains(err.Error(), "not a link") {
		t.Errorf("a link was accepted: %v", err)
	}
}

func TestConnectorsFileErrorsNeverQuoteTheFile(t *testing.T) {
	cases := map[string]string{
		"not json":          `{"uptime_kuma": {"url": "http://x", "api_key": "topsecret-value-1"`,
		"unknown field":     `{"uptime_kuma": {"url": "http://x", "api_key": "topsecret-value-2", "apikey": "x"}}`,
		"user in the url":   `{"uptime_kuma": {"url": "http://user:topsecret-value-3@x", "api_key": "k"}}`,
		"not http":          `{"uptime_kuma": {"url": "ftp://x", "api_key": "topsecret-value-4"}}`,
		"omada incomplete":  `{"omada": {"url": "https://x", "omada_id": "a", "client_id": "b"}}`,
		"keycloak no realm": `{"keycloak": {"url": "https://x", "client_id": "b", "client_secret": "topsecret-value-6"}}`,
		"an unknown system": `{"proxmox": {"token": "topsecret-value-5"}}`,
	}
	for name, body := range cases {
		_, err := LoadConnectors(writeConnectors(t, body, 0o600))
		if err == nil {
			t.Errorf("%s accepted", name)
			continue
		}
		if strings.Contains(err.Error(), "topsecret") {
			t.Errorf("%s: the error quotes the file: %v", name, err)
		}
	}
}

func TestOnlyConfiguredConnectorsAreOffered(t *testing.T) {
	names := func(o Options) string {
		var out []string
		for _, n := range NewRegistry(o).Names() {
			if strings.HasPrefix(n, "kuma.") || strings.HasPrefix(n, "omada.") {
				out = append(out, n)
			}
		}
		return strings.Join(out, ",")
	}
	if got := names(Options{}); got != "" {
		t.Errorf("without a file: %q", got)
	}
	if got := names(Options{Connectors: &Connectors{Kuma: &KumaConfig{URL: "http://x", APIKey: "k"}}}); got != "kuma.monitors" {
		t.Errorf("kuma only: %q", got)
	}
	got := names(Options{Connectors: &Connectors{Omada: &OmadaConfig{URL: "http://x", OmadaID: "o", ClientID: "c", ClientSecret: "s"}}})
	want := "omada.alerts,omada.audit_logs,omada.clients,omada.devices,omada.events,omada.firmware,omada.sites"
	if got != want {
		t.Errorf("omada only: %q, want %q", got, want)
	}
}

// --- Prometheus text ------------------------------------------------------------------------

func TestParseProm(t *testing.T) {
	text := `# HELP monitor_status Monitor Status
# TYPE monitor_status gauge
monitor_status{monitor_id="1",monitor_name="Web \"main\"",monitor_type="http",monitor_url="https://x/",monitor_hostname="null",monitor_port="null"} 1
monitor_response_time{monitor_id="1",monitor_name="a,b} c"} 12.5 1700000000
process_cpu_seconds_total 3.5
broken{a="b" 1
monitor_cert_days_remaining{monitor_id="2"} NaN
empty{} 7
`
	s := parseProm(text)
	if len(s) != 5 {
		t.Fatalf("%d samples: %+v", len(s), s)
	}
	if s[0].Name != "monitor_status" || s[0].Labels["monitor_name"] != `Web "main"` || s[0].Value != 1 {
		t.Errorf("%+v", s[0])
	}
	if s[1].Labels["monitor_name"] != "a,b} c" || s[1].Value != 12.5 {
		t.Errorf("a label may hold , and }: %+v", s[1])
	}
	if s[2].Name != "process_cpu_seconds_total" || len(s[2].Labels) != 0 || s[2].Value != 3.5 {
		t.Errorf("%+v", s[2])
	}
	if s[4].Name != "empty" || s[4].Value != 7 {
		t.Errorf("%+v", s[4])
	}
}

// --- Uptime Kuma ----------------------------------------------------------------------------

const kumaKey = "uk1_abcdef_secretsecretsecret"

const kumaMetricsText = `# HELP monitor_status Monitor Status (1 = UP, 0= DOWN, 2= PENDING, 3= MAINTENANCE)
monitor_status{monitor_id="1",monitor_name="Plex",monitor_type="http",monitor_url="https://plex.example.lan/?token=abc123abc123abc1",monitor_hostname="null",monitor_port="null"} 1
monitor_response_time{monitor_id="1",monitor_name="Plex",monitor_type="http",monitor_url="https://plex.example.lan/?token=abc123abc123abc1",monitor_hostname="null",monitor_port="null"} 42
monitor_cert_days_remaining{monitor_id="1",monitor_name="Plex",monitor_type="http",monitor_url="https://plex.example.lan/?token=abc123abc123abc1",monitor_hostname="null",monitor_port="null"} 12
monitor_cert_is_valid{monitor_id="1",monitor_name="Plex",monitor_type="http",monitor_url="https://plex.example.lan/?token=abc123abc123abc1",monitor_hostname="null",monitor_port="null"} 1
monitor_status{monitor_id="2",monitor_name="NAS ping",monitor_type="ping",monitor_url="null",monitor_hostname="10.0.0.9",monitor_port="null"} 0
monitor_status{monitor_id="3",monitor_name="Backup job",monitor_type="push",monitor_url="https://kuma.example.lan/api/push/PUSHTOKEN1234?status=up",monitor_hostname="null",monitor_port="null"} 2
monitor_status{monitor_id="4",monitor_name="Mail",monitor_type="port",monitor_url="null",monitor_hostname="mail.example.lan",monitor_port="993"} 1
monitor_cert_days_remaining{monitor_id="4",monitor_name="Mail",monitor_type="port",monitor_url="null",monitor_hostname="mail.example.lan",monitor_port="993"} 80
monitor_status{monitor_id="5",monitor_name="Paused",monitor_type="http",monitor_url="https://p/",monitor_hostname="null",monitor_port="null"} 3
monitor_status{monitor_id="6",monitor_name="leaky uk1_abcdef_secretsecretsecret",monitor_type="http",monitor_url="https://user:hunter2hunter2@h/",monitor_hostname="null",monitor_port="null"} 1
`

func kumaServer(t *testing.T, body string, status int) (*httptest.Server, *[]*http.Request) {
	t.Helper()
	var mu sync.Mutex
	var seen []*http.Request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		seen = append(seen, r)
		mu.Unlock()
		_, pw, ok := r.BasicAuth()
		if r.URL.Path != "/metrics" || !ok || pw != kumaKey {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv, &seen
}

func kumaOptions(url string) Options {
	return Options{Connectors: &Connectors{Kuma: &KumaConfig{URL: url, APIKey: kumaKey}}}
}

func TestKumaMonitorsWithProblemsFirstAndNoSecretsInTheOutput(t *testing.T) {
	srv, seen := kumaServer(t, kumaMetricsText, 200)
	out, err := invoke(t, kumaOptions(srv.URL), "kuma.monitors", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	if len(*seen) != 1 {
		t.Fatalf("%d requests", len(*seen))
	}
	if out["total"] != float64(6) || out["problems"] != float64(2) {
		t.Fatalf("%v", out)
	}
	counts := out["counts"].(map[string]any)
	if counts["up"] != float64(3) || counts["down"] != float64(1) || counts["pending"] != float64(1) || counts["maintenance"] != float64(1) {
		t.Fatalf("%v", counts)
	}
	list := out["monitors"].([]any)
	var order []string
	byName := map[string]map[string]any{}
	for _, m := range list {
		mm := m.(map[string]any)
		order = append(order, mm["name"].(string))
		byName[mm["name"].(string)] = mm
	}
	if order[0] != "NAS ping" || order[1] != "Backup job" { // down, then pending
		t.Fatalf("problems first: %v", order)
	}
	plex := byName["Plex"]
	if plex["status"] != "up" || plex["response_ms"] != float64(42) || plex["cert_days_remaining"] != float64(12) || plex["cert_valid"] != true {
		t.Fatalf("%v", plex)
	}
	if plex["url"] != "https://plex.example.lan/" {
		t.Errorf("the query (a token) must not be shown: %v", plex["url"])
	}
	if u := byName["Backup job"]["url"].(string); strings.Contains(u, "PUSHTOKEN1234") || strings.Contains(u, "status=up") {
		t.Errorf("a push token in the path: %q", u)
	}
	if byName["Mail"]["hostname"] != "mail.example.lan" || byName["Mail"]["port"] != "993" {
		t.Errorf("%v", byName["Mail"])
	}
	all := mustJSON(out)
	for _, secret := range []string{kumaKey, "hunter2hunter2", "abc123abc123abc1", "PUSHTOKEN1234"} {
		if strings.Contains(all, secret) {
			t.Errorf("%q in the output: %s", secret, all)
		}
	}
	if !strings.Contains(all, "«masked:connector_secret»") {
		t.Errorf("the key in a monitor name must be replaced: %s", all)
	}
}

func TestKumaFilters(t *testing.T) {
	srv, _ := kumaServer(t, kumaMetricsText, 200)
	o := kumaOptions(srv.URL)
	names := func(in string) []string {
		out, err := invoke(t, o, "kuma.monitors", in)
		if err != nil {
			t.Fatalf("%s: %v", in, err)
		}
		var n []string
		for _, m := range out["monitors"].([]any) {
			n = append(n, m.(map[string]any)["name"].(string))
		}
		return n
	}
	if got := names(`{"status":"down"}`); strings.Join(got, ",") != "NAS ping" {
		t.Errorf("down: %v", got)
	}
	if got := names(`{"name_contains":"PLEX"}`); strings.Join(got, ",") != "Plex" {
		t.Errorf("name: %v", got)
	}
	if got := names(`{"cert_days_below":30}`); strings.Join(got, ",") != "Plex" {
		t.Errorf("certificates: %v", got)
	}
	out, _ := invoke(t, o, "kuma.monitors", `{"limit":2}`)
	if out["truncated"] != true || len(out["monitors"].([]any)) != 2 || out["total"] != float64(6) {
		t.Errorf("%v", out)
	}
	for _, bad := range []string{`{"status":"weird"}`, `{"limit":0,"x":1}`, `{"limit":500}`, `{"cert_days_below":0}`, `{"url":"http://evil"}`} {
		var ie *InputError
		if _, err := invoke(t, o, "kuma.monitors", bad); !errors.As(err, &ie) {
			t.Errorf("%s: want an input error, got %v", bad, err)
		}
	}
}

// Kuma without its own login: /metrics is open (a proxy lets only the tool node in), the file has
// no api_key, and not a single Authorization header goes out.
func TestKumaWithoutAnAPIKeySendsNoCredentials(t *testing.T) {
	var auth []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth = append(auth, r.Header.Get("Authorization"))
		_, _ = w.Write([]byte(kumaMetricsText))
	}))
	defer srv.Close()
	c, err := LoadConnectors(writeConnectors(t, `{"uptime_kuma": {"url": "`+srv.URL+`"}}`, 0o600))
	if err != nil {
		t.Fatalf("a Kuma section without api_key was refused: %v", err)
	}
	if got := c.secrets(); len(got) != 0 {
		t.Fatalf("an empty key would be masked everywhere: %q", got)
	}
	out, err := invoke(t, Options{Connectors: c}, "kuma.monitors", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	if out["total"] != float64(6) {
		t.Fatalf("%v", out)
	}
	if len(auth) != 1 || auth[0] != "" {
		t.Fatalf("an Authorization header was sent without a key: %q", auth)
	}
}

func TestKumaThatWantsALoginTellsWhatToDoWhenThereIsNoKey(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()
	o := Options{Connectors: &Connectors{Kuma: &KumaConfig{URL: srv.URL}}}
	_, err := invoke(t, o, "kuma.monitors", `{}`)
	if err == nil || !strings.Contains(err.Error(), "uptime_kuma.api_key") || strings.Contains(err.Error(), "refused the API key") {
		t.Fatalf("%v", err)
	}
}

func TestKumaRefusalsAreClearAndQuoteNothing(t *testing.T) {
	srv, _ := kumaServer(t, "", 200)
	o := Options{Connectors: &Connectors{Kuma: &KumaConfig{URL: srv.URL, APIKey: "uk1_wrong_keykeykeykey"}}}
	_, err := invoke(t, o, "kuma.monitors", `{}`)
	if err == nil || !strings.Contains(err.Error(), "refused the API key") || strings.Contains(err.Error(), "wrong_key") {
		t.Fatalf("%v", err)
	}
	down, _ := kumaServer(t, "", 200)
	url := down.URL
	down.Close()
	_, err = invoke(t, kumaOptions(url), "kuma.monitors", `{}`)
	if err == nil || !strings.Contains(err.Error(), "not reachable") || strings.Contains(err.Error(), kumaKey) || strings.Contains(err.Error(), url) {
		t.Fatalf("%v", err)
	}
}

func TestAConnectorNeverFollowsARedirectWithItsCredentials(t *testing.T) {
	var leaked []string
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		leaked = append(leaked, r.Header.Get("Authorization"))
	}))
	defer other.Close()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, other.URL+"/metrics", http.StatusFound)
	}))
	defer srv.Close()
	_, err := invoke(t, kumaOptions(srv.URL), "kuma.monitors", `{}`)
	if err == nil || !strings.Contains(err.Error(), "redirect") {
		t.Fatalf("%v", err)
	}
	if len(leaked) != 0 {
		t.Fatalf("the redirect target got a request: %v", leaked)
	}
}

func TestAConnectorReadsAtMostAFewMiB(t *testing.T) {
	big := "monitor_status{monitor_id=\"1\",monitor_name=\"x\"} 1\n" + strings.Repeat("#"+strings.Repeat("a", 1000)+"\n", 5000)
	srv, _ := kumaServer(t, big, 200)
	if _, err := invoke(t, kumaOptions(srv.URL), "kuma.monitors", `{}`); err == nil || !strings.Contains(err.Error(), "more than") {
		t.Fatalf("%v", err)
	}
}

func TestTLSOfAConnector(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`monitor_status{monitor_id="1",monitor_name="x"} 1` + "\n"))
	}))
	defer srv.Close()
	plain := Options{Connectors: &Connectors{Kuma: &KumaConfig{URL: srv.URL, APIKey: kumaKey}}}
	if _, err := invoke(t, plain, "kuma.monitors", `{}`); err == nil {
		t.Error("a certificate nobody trusts was accepted")
	}
	insecure := Options{Connectors: &Connectors{Kuma: &KumaConfig{URL: srv.URL, APIKey: kumaKey, TLS: TLS{Insecure: true}}}}
	if _, err := invoke(t, insecure, "kuma.monitors", `{}`); err != nil {
		t.Errorf("insecure_skip_verify: %v", err)
	}
	ca := filepath.Join(t.TempDir(), "ca.pem")
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw})
	if err := os.WriteFile(ca, pemBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	withCA := Options{Connectors: &Connectors{Kuma: &KumaConfig{URL: srv.URL, APIKey: kumaKey, TLS: TLS{CAFile: ca}}}}
	if _, err := invoke(t, withCA, "kuma.monitors", `{}`); err != nil {
		t.Errorf("ca_file: %v", err)
	}
}

// sharedInvoke is invoke on ONE registry, like a running node (the Omada client keeps its token).
func sharedInvoke(t *testing.T, o Options) func(name, input string) (map[string]any, error) {
	t.Helper()
	r := NewRegistry(o)
	return func(name, input string) (map[string]any, error) {
		t.Helper()
		c, ok := r.Get(name)
		if !ok {
			t.Fatalf("no capability %s", name)
		}
		raw, err := c.Invoke(context.Background(), json.RawMessage(input))
		if err != nil {
			return nil, err
		}
		var out map[string]any
		if err := json.Unmarshal(raw, &out); err != nil {
			t.Fatalf("not a JSON object: %s", raw)
		}
		return out, nil
	}
}

// --- Omada ----------------------------------------------------------------------------------

type fakeOmada struct {
	t        *testing.T
	mu       sync.Mutex
	tokens   int
	calls    []string
	expire   bool // answer -44112 once
	sites    string
	override map[string]string // path -> result JSON
}

func (f *fakeOmada) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		reply := func(code int, msg, result string) {
			if result == "" {
				result = "null"
			}
			_, _ = fmt.Fprintf(w, `{"errorCode":%d,"msg":%q,"result":%s}`, code, msg, result)
		}
		if r.URL.Path == "/openapi/authorize/token" {
			var body map[string]string
			_ = json.NewDecoder(r.Body).Decode(&body)
			if r.Method != "POST" || r.URL.Query().Get("grant_type") != "client_credentials" ||
				body["omadacId"] != "oid123" || body["client_id"] != "cid-12345" || body["client_secret"] != "csecret-67890" {
				reply(-44106, "bad credentials csecret-67890", "")
				return
			}
			f.tokens++
			reply(0, "Success.", fmt.Sprintf(`{"accessToken":"tok-%d-secrettoken","tokenType":"bearer","expiresIn":7200,"refreshToken":"r"}`, f.tokens))
			return
		}
		f.calls = append(f.calls, r.URL.RequestURI())
		if !strings.HasPrefix(r.Header.Get("Authorization"), "AccessToken=tok-") {
			reply(-44112, "The access token has expired or is invalid.", "")
			return
		}
		if f.expire {
			f.expire = false
			reply(-44112, "The access token has expired or is invalid.", "")
			return
		}
		p := strings.TrimPrefix(r.URL.Path, "/openapi/v1/oid123")
		if res, ok := f.override[p]; ok {
			reply(0, "Success.", res)
			return
		}
		switch p {
		case "/sites":
			reply(0, "Success.", f.sites)
		case "/sites/s1/devices":
			reply(0, "Success.", `{"totalRows":2,"currentPage":1,"currentSize":2,"data":[
				{"name":"AP Living","mac":"AA-BB-CC-DD-EE-01","model":"EAP660 HD","type":"ap","status":1,"ip":"10.0.0.21","firmwareVersion":"1.2.3","uptime":"3day(s) 4h","wlanPassword":"hunter2hunter2","adminToken":"x","nested":{"a":1}},
				{"name":"Switch","mac":"AA-BB-CC-DD-EE-02","model":"SG2008P","type":"switch","status":0}]}`)
		case "/sites/s1/clients":
			reply(0, "Success.", `{"totalRows":1,"data":[{"name":"laptop","ip":"10.0.0.50","mac":"11-22-33-44-55-66","wireless":true,"signalLevel":-55}]}`)
		case "/sites/s1/logs/events", "/sites/s1/logs/alerts":
			// the real controller (2026-11-17): no time window, a framework 400 without errorCode
			q := r.URL.Query()
			if q.Get("filters.timeStart") == "" || q.Get("filters.timeEnd") == "" {
				w.WriteHeader(http.StatusBadRequest)
				_, _ = fmt.Fprintf(w, `{"timestamp":1,"status":400,"error":"Bad Request","path":%q}`, r.URL.Path)
				return
			}
			if p == "/sites/s1/logs/alerts" {
				reply(0, "Success.", `{"totalRows":0,"data":[]}`)
				return
			}
			rows := `{"key":"L_C_DISCONN","module":"Client","content":"[client:26-8B-AE-4A-79-D9] was disconnected from SSID \"Home 5G\" on [ap:Outdoor AP]","time":1794900000000},` +
				`{"key":"L_C_CONN","module":"Client","content":"[client:11-22-33-44-55-66] went online on [switch:Switch]","time":1794890000000}`
			if q.Get("page") != "1" {
				rows = ""
			}
			reply(0, "Success.", `{"totalRows":2,"data":[`+rows+`]}`)
		case "/sites/s1/devices/AA-BB-CC-DD-EE-01/latest-firmware-info":
			reply(0, "Success.", `{"curFwVer":"1.2.3","lastFwVer":"1.3.0","fwReleaseLog":"fix csecret-67890 leak"}`)
		default:
			reply(-1600, "Unsupported path "+p, "")
		}
	})
}

func omadaOptions(url, site string) Options {
	return Options{Connectors: &Connectors{Omada: &OmadaConfig{
		URL: url, OmadaID: "oid123", ClientID: "cid-12345", ClientSecret: "csecret-67890", Site: site,
	}}}
}

const oneSite = `{"totalRows":1,"data":[{"siteId":"s1","name":"Home"}]}`

func TestOmadaDevicesKeepTheControllersFieldsAndDropSecrets(t *testing.T) {
	f := &fakeOmada{t: t, sites: oneSite}
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	call := sharedInvoke(t, omadaOptions(srv.URL, ""))
	out, err := call("omada.devices", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	if out["site"] != "Home" || out["total"] != float64(2) || out["truncated"] != false {
		t.Fatalf("%v", out)
	}
	items := out["items"].([]any)
	ap := items[0].(map[string]any)
	if ap["name"] != "AP Living" || ap["firmwareVersion"] != "1.2.3" || ap["status"] != float64(1) || ap["ip"] != "10.0.0.21" {
		t.Fatalf("%v", ap)
	}
	for _, dropped := range []string{"wlanPassword", "adminToken", "nested"} {
		if _, has := ap[dropped]; has {
			t.Errorf("%s was listed", dropped)
		}
	}
	all := mustJSON(out)
	if strings.Contains(all, "hunter2hunter2") || strings.Contains(all, "csecret-67890") || strings.Contains(all, "secrettoken") {
		t.Errorf("a secret in the output: %s", all)
	}
	// the token is reused for the next call, the paths are the ones of the controller
	if _, err := call("omada.clients", `{"limit":10}`); err != nil {
		t.Fatal(err)
	}
	if f.tokens != 1 {
		t.Errorf("%d token requests, want 1", f.tokens)
	}
	want := []string{
		"/openapi/v1/oid123/sites?page=1&pageSize=100",
		"/openapi/v1/oid123/sites/s1/devices?page=1&pageSize=50",
		"/openapi/v1/oid123/sites/s1/clients?page=1&pageSize=10",
	}
	if strings.Join(f.calls, "\n") != strings.Join(want, "\n") {
		t.Errorf("calls:\n%s", strings.Join(f.calls, "\n"))
	}
}

func TestOmadaLogsInAgainOnceWhenTheTokenIsGone(t *testing.T) {
	f := &fakeOmada{t: t, sites: oneSite}
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	call := sharedInvoke(t, omadaOptions(srv.URL, ""))
	if _, err := call("omada.sites", `{}`); err != nil {
		t.Fatal(err)
	}
	f.expire = true
	if _, err := call("omada.clients", `{}`); err != nil {
		t.Fatalf("%v", err)
	}
	if f.tokens != 2 {
		t.Errorf("%d token requests, want 2", f.tokens)
	}
}

func TestOmadaSitesAreChosenFromTheControllersOwnList(t *testing.T) {
	two := `{"totalRows":2,"data":[{"siteId":"s1","name":"Home"},{"siteId":"s2","name":"Garage"}]}`
	f := &fakeOmada{t: t, sites: two}
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	var ie *InputError
	if _, err := invoke(t, omadaOptions(srv.URL, ""), "omada.devices", `{}`); !errors.As(err, &ie) || !strings.Contains(err.Error(), "Garage, Home") {
		t.Fatalf("two sites and none named: %v", err)
	}
	if out, err := invoke(t, omadaOptions(srv.URL, ""), "omada.sites", `{}`); err != nil || out["total"] != float64(2) {
		t.Errorf("listing the sites needs no site: %v %v", out, err)
	}
	if _, err := invoke(t, omadaOptions(srv.URL, "home"), "omada.devices", `{}`); err != nil {
		t.Errorf("the configured site, any case: %v", err)
	}
	if _, err := invoke(t, omadaOptions(srv.URL, ""), "omada.devices", `{"site":"s1"}`); err != nil {
		t.Errorf("by id: %v", err)
	}
	if _, err := invoke(t, omadaOptions(srv.URL, "Home"), "omada.devices", `{"site":"../../x"}`); !errors.As(err, &ie) {
		t.Errorf("a site the controller does not list: %v", err)
	}
	for _, c := range f.calls {
		if strings.Contains(c, "..") {
			t.Errorf("a path with the model's text reached the controller: %s", c)
		}
	}
}

func TestOmadaFirmwareTakesADevicesMACAndNothingElse(t *testing.T) {
	f := &fakeOmada{t: t, sites: oneSite}
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	o := omadaOptions(srv.URL, "")
	out, err := invoke(t, o, "omada.firmware", `{"device_mac":"aa:bb:cc:dd:ee:01"}`)
	if err != nil {
		t.Fatal(err)
	}
	fw := out["firmware"].(map[string]any)
	if out["device_mac"] != "AA-BB-CC-DD-EE-01" || fw["curFwVer"] != "1.2.3" || fw["lastFwVer"] != "1.3.0" {
		t.Fatalf("%v", out)
	}
	if strings.Contains(mustJSON(out), "csecret-67890") {
		t.Error("the client secret echoed by the controller reached the output")
	}
	var ie *InputError
	for _, bad := range []string{`{"device_mac":"../etc/passwd"}`, `{"device_mac":"AA-BB"}`, `{}`} {
		if _, err := invoke(t, o, "omada.firmware", bad); !errors.As(err, &ie) {
			t.Errorf("%s: %v", bad, err)
		}
	}
}

func TestOmadaErrorsAreTheControllersAndQuoteNoCredentials(t *testing.T) {
	f := &fakeOmada{t: t, sites: oneSite}
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	// a path the controller does not know: its own error code comes back
	_, err := invoke(t, omadaOptions(srv.URL, ""), "omada.audit_logs", `{}`)
	if err == nil || !strings.Contains(err.Error(), "-1600") {
		t.Fatalf("%v", err)
	}
	// wrong credentials: the controller's message may quote the secret, the error must not
	bad := omadaOptions(srv.URL, "")
	bad.Connectors.Omada.ClientSecret = "csecret-67890x"
	_, err = invoke(t, bad, "omada.sites", `{}`)
	if err == nil || !strings.Contains(err.Error(), "refused the client credentials") {
		t.Fatalf("%v", err)
	}
	if strings.Contains(err.Error(), "csecret-67890x") {
		t.Errorf("the secret is in the error: %v", err)
	}
}

// A connector capability takes filters only: no input can carry a URL, a host or a credential, so a
// model can neither aim a connector elsewhere nor hand it (or ask it for) a secret.
func TestConnectorCapabilitiesTakeFiltersOnly(t *testing.T) {
	o := Options{Connectors: &Connectors{
		Kuma:  &KumaConfig{URL: "http://x", APIKey: "k"},
		Omada: &OmadaConfig{URL: "http://x", OmadaID: "o", ClientID: "c", ClientSecret: "s"},
	}}
	r := NewRegistry(o)
	n := 0
	for _, name := range r.Names() {
		if !strings.HasPrefix(name, "kuma.") && !strings.HasPrefix(name, "omada.") {
			continue
		}
		n++
		c, _ := r.Get(name)
		info := capability.InfoOf(c)
		if !info.ReadOnly || len(info.Description) < 30 {
			t.Errorf("%s: %+v", name, info)
		}
		var schema map[string]any
		if err := json.Unmarshal(c.InputSchema(), &schema); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if schema["additionalProperties"] != false {
			t.Errorf("%s accepts unknown fields", name)
		}
		for prop := range schema["properties"].(map[string]any) {
			low := strings.ToLower(prop)
			for _, bad := range []string{"url", "host", "key", "secret", "token", "pass", "auth", "credential", "path", "command"} {
				if strings.Contains(low, bad) {
					t.Errorf("%s has an input called %q", name, prop)
				}
			}
		}
	}
	if n != 8 {
		t.Errorf("%d connector capabilities, want 8", n)
	}
}

func TestOmadaLogsAskWithATimeWindowAndFilterByMatch(t *testing.T) {
	f := &fakeOmada{t: t, sites: oneSite}
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	call := sharedInvoke(t, omadaOptions(srv.URL, ""))
	out, err := call("omada.events", `{"since":"2d","match":"26:8b:ae:4a:79:d9|nothing"}`)
	if err != nil {
		t.Fatal(err)
	}
	if out["matched"] != float64(1) || out["scanned"] != float64(2) || out["since"] == nil || out["until"] == nil {
		t.Fatalf("%v", out)
	}
	row := out["items"].([]any)[0].(map[string]any)
	if row["key"] != "L_C_DISCONN" {
		t.Fatalf("%v", row)
	}
	if ts, _ := row["time"].(string); !strings.HasPrefix(ts, "2026-") {
		t.Errorf("time not readable: %v", row["time"])
	}
	last := f.calls[len(f.calls)-1]
	if !strings.Contains(last, "filters.timeStart=") || !strings.Contains(last, "filters.timeEnd=") {
		t.Errorf("no time window: %s", last)
	}
	// the default window is the last day; alerts take one too
	if out, err := call("omada.alerts", `{}`); err != nil || out["total"] != float64(0) {
		t.Fatalf("alerts: %v %v", out, err)
	}
	// a match that finds nothing says why it may have missed
	out, err = call("omada.events", `{"match":"Luxel"}`)
	if err != nil || out["matched"] != float64(0) || !strings.Contains(fmt.Sprint(out["note"]), "names in the controller may differ") {
		t.Fatalf("%v %v", out, err)
	}
}

func TestOmadaTimeWindowOnlyOnLogs(t *testing.T) {
	f := &fakeOmada{t: t, sites: oneSite}
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	var ie *InputError
	_, err := invoke(t, omadaOptions(srv.URL, ""), "omada.clients", `{"since":"1d"}`)
	if !errors.As(err, &ie) || !strings.Contains(err.Error(), "omada.events") {
		t.Fatalf("since on clients: %v", err)
	}
	if _, err := invoke(t, omadaOptions(srv.URL, ""), "omada.events", `{"since":"1h","until":"2h"}`); !errors.As(err, &ie) {
		t.Errorf("since after until: %v", err)
	}
	if _, err := invoke(t, omadaOptions(srv.URL, ""), "omada.firmware", `{"device_mac":"AA-BB-CC-DD-EE-01","match":"x"}`); err == nil {
		t.Errorf("match on firmware was taken")
	}
}

func TestOmadaClientsAreCompactAndFoundByMatch(t *testing.T) {
	f := &fakeOmada{t: t, sites: oneSite, override: map[string]string{
		"/sites/s1/clients": `{"totalRows":2,"data":[
			{"name":"Pixel-7","hostName":"Pixel-7","mac":"26-8B-AE-4A-79-D9","ssid":"Home 5G","apName":"Lu AP","rssi":-36,"lastSeen":1794900000000,"downPacket":1,"trafficDown":2,"radioId":1,"activity":0},
			{"name":"laptop","mac":"11-22-33-44-55-66","ssid":"Home","apName":"Outdoor AP","rssi":-70}]}`,
	}}
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	out, err := invoke(t, omadaOptions(srv.URL, ""), "omada.clients", `{"match":"pixel"}`)
	if err != nil {
		t.Fatal(err)
	}
	items := out["items"].([]any)
	if len(items) != 1 {
		t.Fatalf("%v", out)
	}
	row := items[0].(map[string]any)
	if row["apName"] != "Lu AP" || row["rssi"] != float64(-36) || row["hostName"] != "Pixel-7" {
		t.Fatalf("%v", row)
	}
	for _, gone := range []string{"downPacket", "trafficDown", "activity"} {
		if _, has := row[gone]; has {
			t.Errorf("%s kept in a client row", gone)
		}
	}
	if ts, _ := row["lastSeen"].(string); !strings.HasPrefix(ts, "2026-") {
		t.Errorf("lastSeen not readable: %v", row["lastSeen"])
	}
}

func TestOmadaHTTPErrorSaysWhatTheControllerSaid(t *testing.T) {
	err := omadaHTTPError(400, []byte(`{"timestamp":1,"status":400,"error":"Bad Request","path":"/x"}`))
	if !strings.Contains(err.Error(), "HTTP 400 (Bad Request)") || !strings.Contains(err.Error(), "fault of the tool") {
		t.Errorf("%v", err)
	}
	if err := omadaHTTPError(502, []byte("<html>")); err.Error() != "the Omada controller answered HTTP 502" {
		t.Errorf("%v", err)
	}
}

func TestOmadaEventsSummarizeTheWholeWindow(t *testing.T) {
	f := &fakeOmada{t: t, sites: oneSite}
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	out, err := invoke(t, omadaOptions(srv.URL, ""), "omada.events", `{"match":"nothing-like-this"}`)
	if err != nil {
		t.Fatal(err)
	}
	w := out["window"].(map[string]any)
	on := w["client_events_on"].(map[string]any)
	if w["events"] != float64(2) || w["client_events"] != float64(2) || on["ap"] != float64(1) || on["switch"] != float64(1) || w["note"] != nil {
		t.Fatalf("%v", w)
	}
	// only wired client events, and an AP's own event: the log cannot show a Wi-Fi drop
	wired := &fakeOmada{t: t, sites: oneSite, override: map[string]string{"/sites/s1/logs/events": `{"totalRows":2,"data":[
		{"key":"L_C_CONN","content":"[client:11-22-33-44-55-66] went online on [switch:Switch]","time":1794890000000},
		{"key":"DEV_CONN","content":"[ap:1C-61-B4-FD-26-72] was connected.","time":1794890000000}]}`}}
	srv2 := httptest.NewServer(wired.handler())
	defer srv2.Close()
	out, err = invoke(t, omadaOptions(srv2.URL, ""), "omada.events", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	w = out["window"].(map[string]any)
	if w["client_events"] != float64(1) || !strings.Contains(fmt.Sprint(w["note"]), "not in this log") {
		t.Fatalf("%v", w)
	}
	if k := w["by_key"].(map[string]any); k["DEV_CONN"] != float64(1) || k["L_C_CONN"] != float64(1) {
		t.Errorf("%v", k)
	}
}

// issue #226: a guessed or garbled site name on a controller with one site reads that site, and
// the answer says so.
func TestOmadaAnUnknownSiteFallsBackToTheOnlySite(t *testing.T) {
	f := &fakeOmada{t: t, sites: oneSite}
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	for _, asked := range []string{"default", "Z..??"} {
		out, err := invoke(t, omadaOptions(srv.URL, ""), "omada.devices", fmt.Sprintf(`{"site":%q}`, asked))
		if err != nil {
			t.Fatalf("%q: %v", asked, err)
		}
		note, _ := out["site_note"].(string)
		if out["site"] != "Home" || !strings.Contains(note, asked) || !strings.Contains(note, `"Home"`) {
			t.Errorf("%q: the answer must name the site read: %v", asked, out)
		}
	}
	// a configured site the controller no longer has: the only one as well
	if out, err := invoke(t, omadaOptions(srv.URL, "Old"), "omada.devices", `{}`); err != nil || out["site"] != "Home" {
		t.Errorf("configured site gone: %v %v", out, err)
	}
	// the right name: no note
	if out, err := invoke(t, omadaOptions(srv.URL, ""), "omada.devices", `{"site":"home"}`); err != nil || out["site_note"] != nil {
		t.Errorf("the right name needs no note: %v %v", out, err)
	}
	for _, c := range f.calls {
		if strings.Contains(c, "default") || strings.Contains(c, "Z..") {
			t.Errorf("the asked name reached the controller: %s", c)
		}
	}
}

// issue #226: a site name with characters beyond ASCII reaches the controller lookup whole,
// whether the JSON carries it as UTF-8 or escaped, and one whose characters were lost on the way
// still names the one site it fits.
func TestOmadaANonASCIISiteNameIsReadWhole(t *testing.T) {
	two := `{"totalRows":2,"data":[{"siteId":"s1","name":"Zuhause Süd"},{"siteId":"s2","name":"Zuhause Nord"}]}`
	f := &fakeOmada{t: t, sites: two}
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	for _, in := range []string{`{"site":"Zuhause Süd"}`, `{"site":"zuhause süd"}`, `{"site":"Zuhause S??d"}`, `{"site":"Zuhause S�d"}`} {
		out, err := invoke(t, omadaOptions(srv.URL, ""), "omada.devices", in)
		if err != nil || out["total"] != float64(2) {
			t.Errorf("%s: %v %v", in, out, err)
		}
	}
	var ie *InputError
	// two sites, a name that fits neither: the error lists both, nothing is guessed
	if _, err := invoke(t, omadaOptions(srv.URL, ""), "omada.devices", `{"site":"default"}`); !errors.As(err, &ie) || !strings.Contains(err.Error(), "Zuhause Nord, Zuhause Süd") {
		t.Errorf("two sites, unknown name: %v", err)
	}
	// a mark stands for characters beyond ASCII only: "Zuhause ?ord" is no site
	if _, err := invoke(t, omadaOptions(srv.URL, ""), "omada.devices", `{"site":"Zuhause ?ord"}`); !errors.As(err, &ie) {
		t.Errorf("a mark for an ASCII letter: %v", err)
	}
}

func TestOmadaCutKeepsCharactersWhole(t *testing.T) {
	got := cut("Süd", 2) // "S" + the first byte of "ü"
	if got != "S…" || !utf8.ValidString(got) {
		t.Errorf("cut = %q", got)
	}
}

// review of #226: the asked name comes from the model, so matching it must not backtrack
// exponentially ("?ü" 32 times against a long run of "ü" took minutes before).
func TestOmadaGarbledFitsStaysFast(t *testing.T) {
	done := make(chan bool, 1)
	go func() { done <- garbledFits(strings.Repeat("?ü", 32), strings.Repeat("ü", 200)+"x") }()
	select {
	case got := <-done:
		if got {
			t.Error("a name ending in x fits no name of marks and ü")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("garbledFits backtracks exponentially")
	}
	if !garbledFits("?ü?", "üüüü") || garbledFits("?ü?", "ü") || !garbledFits("Zuhause S??d", "zuhause süd") {
		t.Error("garbledFits rules changed")
	}
}
