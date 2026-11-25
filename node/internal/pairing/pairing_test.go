package pairing

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"topcoat/node/internal/hubtls"
	"topcoat/node/internal/hubtls/hubtlstest"
	"topcoat/node/internal/schematest"
	"topcoat/node/internal/store"
)

const nodeID = "3f0c2a4e-8a51-4b7e-9a0e-1f2d3c4b5a60"

func TestSanitizeName(t *testing.T) {
	cases := map[string]string{
		"Gaming-PC":                    "gaming-pc",
		"node_amd.example.lan":         "node-amd",
		"--Weird  Name!!--":            "weird-name",
		"":                             "node",
		"ÄÖÜ":                          "node",
		strings.Repeat("a", 80):        strings.Repeat("a", 63),
		"a" + strings.Repeat("-b", 40): ("a" + strings.Repeat("-b", 40))[:63],
	}
	for in, want := range cases {
		got := SanitizeName(in)
		if got != want {
			t.Errorf("%q: got %q want %q", in, got, want)
		}
		if !ValidName(got) {
			t.Errorf("%q: result %q invalid", in, got)
		}
	}
}

type hub struct {
	status int
	body   string
	got    []byte
	path   string
}

func (h *hub) server(t *testing.T) *httptest.Server {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.path = r.URL.Path
		if r.Method != http.MethodPost || r.Header.Get("Content-Type") != "application/json" {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		h.got, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(h.status)
		_, _ = w.Write([]byte(h.body))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func openDir(t *testing.T) *store.Dir {
	d, err := store.Open(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestPairSuccess(t *testing.T) {
	h := &hub{status: 200, body: `{"node_id":"` + nodeID + `"}`}
	srv := h.server(t)
	d := openDir(t)
	id, err := Pair(context.Background(), Options{
		HubURL:       srv.URL + "/",
		InsecureHTTP: true,
		Code:         "abcd-1234",
		AgentVersion: "0.1.0-test",
		Dir:          d,
		Hostname:     func() (string, error) { return "Gaming-PC.example.lan", nil },
		GOOS:         "linux",
		GOARCH:       "amd64",
	})
	if err != nil {
		t.Fatal(err)
	}
	if h.path != "/api/node/pair" {
		t.Fatalf("path %s", h.path)
	}
	if err := schematest.Validate("pair_request", h.got); err != nil {
		t.Fatalf("request violates schema: %v\n%s", err, h.got)
	}
	if err := schematest.Validate("pair_response", []byte(h.body)); err != nil {
		t.Fatal(err)
	}
	var req Request
	_ = json.Unmarshal(h.got, &req)
	if req.Code != "ABCD-1234" || req.Name != "gaming-pc" || req.OS != "linux" || req.Arch != "amd64" || req.AgentVersion != "0.1.0-test" {
		t.Fatalf("request %+v", req)
	}
	if id.NodeID != nodeID || id.HubURL != srv.URL || !id.InsecureHTTP {
		t.Fatalf("identity %+v", id)
	}
	loaded, err := d.LoadIdentity()
	if err != nil || loaded != id {
		t.Fatalf("stored identity %+v %v", loaded, err)
	}
	key, err := d.LoadPrivateKey()
	if err != nil {
		t.Fatal(err)
	}
	pub, _ := base64.StdEncoding.DecodeString(req.PublicKey)
	if !key.Public().(ed25519.PublicKey).Equal(ed25519.PublicKey(pub)) {
		t.Fatal("stored key does not match the public key sent")
	}
	if runtime.GOOS != "windows" {
		for _, f := range []string{store.IdentityFile, store.KeyFile} {
			st, err := os.Stat(filepath.Join(d.Path, f))
			if err != nil {
				t.Fatal(err)
			}
			if st.Mode().Perm() != 0o600 {
				t.Fatalf("%s mode %v", f, st.Mode().Perm())
			}
		}
	}
	// Second pair without --force is refused.
	if _, err := Pair(context.Background(), Options{HubURL: srv.URL, InsecureHTTP: true, Code: "ABCD-1234", Name: "x", Dir: d}); err == nil {
		t.Fatal("re-pair without force accepted")
	}
	if _, err := Pair(context.Background(), Options{HubURL: srv.URL, InsecureHTTP: true, Code: "ABCD-1234", Name: "x", Dir: d, Force: true}); err != nil {
		t.Fatalf("re-pair with force: %v", err)
	}
}

func TestPairErrors(t *testing.T) {
	cases := []struct {
		status int
		body   string
		want   string
	}{
		{404, `{"detail":"unknown code"}`, "not found: unknown code"},
		{410, `{"detail":"code expired"}`, "expired or already used: code expired"},
		{400, `{"detail":[{"msg":"bad"}]}`, `[{"msg":"bad"}]`},
		{500, `oops`, "HTTP 500: oops"},
		{200, `{"node_id":"nope"}`, "missing a valid node_id"},
		{200, `not json`, "parse hub response"},
	}
	for _, c := range cases {
		h := &hub{status: c.status, body: c.body}
		srv := h.server(t)
		d := openDir(t)
		_, err := Pair(context.Background(), Options{HubURL: srv.URL, InsecureHTTP: true, Code: "ABCD-1234", Name: "node-amd", Dir: d})
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%d %s: got %v want %q", c.status, c.body, err, c.want)
		}
		var he *HubError
		if c.status != 200 && !errors.As(err, &he) {
			t.Errorf("%d: not a HubError", c.status)
		}
		if d.HasIdentity() {
			t.Errorf("%d: identity written on failure", c.status)
		}
	}
}

func TestPairValidation(t *testing.T) {
	d := openDir(t)
	bad := []Options{
		{HubURL: "http://hub.example.lan:8000", Code: "abcd", Name: "n", Dir: d},
		{HubURL: "http://hub.example.lan:8000", Code: "ABCD-1234", Name: "Bad Name", Dir: d},
		{HubURL: "ftp://hub.example.lan", Code: "ABCD-1234", Name: "n", Dir: d},
		{HubURL: "http://", Code: "ABCD-1234", Name: "n", Dir: d},
		{HubURL: "http://hub.example.lan:8000", Code: "ABCD-1234", Name: "n"},
		{HubURL: "http://hub.example.lan:8000", Code: "ABCD-1234", Dir: d, Hostname: func() (string, error) { return "", errors.New("x") }},
	}
	for i, o := range bad {
		if _, err := Pair(context.Background(), o); err == nil {
			t.Errorf("case %d accepted", i)
		}
	}
	if u, err := PairURL("https://hub.example.lan/base/"); err != nil || u != "https://hub.example.lan/base/api/node/pair" {
		t.Fatalf("%s %v", u, err)
	}
}

func tlsHub(t *testing.T, names ...string) *hubtlstest.Server {
	return hubtlstest.NewServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"node_id":"` + nodeID + `"}`))
	}), names...)
}

func TestPairPinsHubCA(t *testing.T) {
	srv := tlsHub(t, "127.0.0.1")
	d := openDir(t)
	id, err := Pair(context.Background(), Options{
		HubURL: srv.URL, Code: "ABCD-1234", Name: "node-amd", Dir: d,
		CAFingerprint: hubtls.Fingerprint(srv.CA.Raw),
	})
	if err != nil {
		t.Fatal(err)
	}
	if id.InsecureHTTP {
		t.Fatal("https pairing marked insecure")
	}
	ca, ok, err := d.LoadHubCA()
	if err != nil || !ok || string(ca) != string(srv.CAPEM) {
		t.Fatalf("hub CA not stored: %v %v", ok, err)
	}
	// Pairing again with system trust removes the pinned CA.
	pub := tlsHub(t, "127.0.0.1")
	_, err = Pair(context.Background(), Options{HubURL: pub.URL, Code: "ABCD-1234", Name: "node-amd", Dir: d, Force: true})
	if err == nil || !strings.Contains(err.Error(), "--ca-fingerprint") {
		t.Fatalf("private CA accepted via system trust: %v", err)
	}
}

func TestPairRefusesWrongFingerprintOrHost(t *testing.T) {
	srv := tlsHub(t, "127.0.0.1")
	d := openDir(t)
	_, err := Pair(context.Background(), Options{
		HubURL: srv.URL, Code: "ABCD-1234", Name: "node-amd", Dir: d,
		CAFingerprint: "sha256:" + strings.Repeat("11", 32),
	})
	if err == nil || !strings.Contains(err.Error(), "does not contain the CA") {
		t.Fatalf("wrong fingerprint: %v", err)
	}
	other := tlsHub(t, "hub.example.lan")
	_, err = Pair(context.Background(), Options{
		HubURL: other.URL, Code: "ABCD-1234", Name: "node-amd", Dir: d,
		CAFingerprint: hubtls.Fingerprint(other.CA.Raw),
	})
	if err == nil || !strings.Contains(err.Error(), "TOPCOAT_TLS_HOSTNAMES") {
		t.Fatalf("host mismatch: %v", err)
	}
	if d.HasIdentity() {
		t.Fatal("identity written after a failed TLS check")
	}
	if _, ok, _ := d.LoadHubCA(); ok {
		t.Fatal("CA written after a failed TLS check")
	}
}

func TestPairWithCAFile(t *testing.T) {
	srv := tlsHub(t, "127.0.0.1")
	caFile := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(caFile, srv.CAPEM, 0o600); err != nil {
		t.Fatal(err)
	}
	d := openDir(t)
	if _, err := Pair(context.Background(), Options{HubURL: srv.URL, Code: "ABCD-1234", Name: "n", Dir: d, CAFile: caFile}); err != nil {
		t.Fatal(err)
	}
	if ca, ok, _ := d.LoadHubCA(); !ok || string(ca) != string(srv.CAPEM) {
		t.Fatal("CA file not stored")
	}
}

func TestPairTrustOptionErrors(t *testing.T) {
	d := openDir(t)
	fp := "sha256:" + strings.Repeat("11", 32)
	cases := []struct {
		o    Options
		want string
	}{
		{Options{HubURL: "http://hub.example.lan:8000"}, "plain http"},
		{Options{HubURL: "http://hub.example.lan:8000", InsecureHTTP: true, CAFingerprint: fp}, "need an https"},
		{Options{HubURL: "https://hub.example.lan:8443", CAFingerprint: fp, CAFile: "x.pem"}, "either"},
		{Options{HubURL: "https://hub.example.lan:8443", CAFingerprint: "sha256:12"}, "64 hex"},
		{Options{HubURL: "https://hub.example.lan:8443", CAFile: filepath.Join(t.TempDir(), "missing.pem")}, "--ca-file"},
	}
	for i, c := range cases {
		c.o.Code, c.o.Name, c.o.Dir = "ABCD-1234", "n", d
		if _, err := Pair(context.Background(), c.o); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("case %d: got %v want %q", i, err, c.want)
		}
	}
}

func TestPairSendsTheRoleAndOmitsTheDefault(t *testing.T) {
	for role, want := range map[string]string{"": "", "tools": "tools", "gpu": "gpu"} {
		h := &hub{status: 200, body: `{"node_id":"` + nodeID + `"}`}
		srv := h.server(t)
		_, err := Pair(context.Background(), Options{
			HubURL: srv.URL, InsecureHTTP: true, Code: "ABCD-1234", Role: role, Dir: openDir(t),
			Hostname: func() (string, error) { return "vm1", nil }, GOOS: "linux", GOARCH: "amd64",
		})
		if err != nil {
			t.Fatal(err)
		}
		if err := schematest.Validate("pair_request", h.got); err != nil {
			t.Fatalf("role %q violates the schema: %v\n%s", role, err, h.got)
		}
		var req Request
		_ = json.Unmarshal(h.got, &req)
		if req.Role != want || (want == "" && strings.Contains(string(h.got), `"role"`)) {
			t.Fatalf("role %q: %s", role, h.got)
		}
	}
}
