package hubtls_test

import (
	"errors"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/hubtls"
	"topcoat/node/internal/hubtls/hubtlstest"
	"topcoat/node/internal/store"
)

var ok = http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok")) })

func get(t *testing.T, c *http.Client, url string) error {
	t.Helper()
	resp, err := c.Get(url)
	if err != nil {
		return err
	}
	_ = resp.Body.Close()
	return nil
}

func TestParseFingerprint(t *testing.T) {
	hex := strings.Repeat("ab", 32)
	for _, in := range []string{"sha256:" + hex, "SHA256:" + strings.ToUpper(hex), " " + hex + " ",
		"sha256:" + strings.TrimSuffix(strings.Repeat("ab:", 32), ":")} {
		if b, err := hubtls.ParseFingerprint(in); err != nil || len(b) != 32 || b[0] != 0xab {
			t.Errorf("%q: %v %x", in, err, b)
		}
	}
	for _, in := range []string{"", "sha256:", "sha256:abcd", "sha1:" + hex, "sha256:" + hex + "00", "zz"} {
		if _, err := hubtls.ParseFingerprint(in); err == nil {
			t.Errorf("%q accepted", in)
		}
	}
}

func TestCheckScheme(t *testing.T) {
	if useTLS, err := hubtls.CheckScheme("https://hub.example.lan:8443", false); err != nil || !useTLS {
		t.Fatal(useTLS, err)
	}
	if _, err := hubtls.CheckScheme("http://hub.example.lan:8000", false); !errors.Is(err, hubtls.ErrInsecureHTTP) {
		t.Fatal(err)
	}
	if useTLS, err := hubtls.CheckScheme("http://127.0.0.1:1", true); err != nil || useTLS {
		t.Fatal(useTLS, err)
	}
	if _, err := hubtls.CheckScheme("ftp://x", true); err == nil {
		t.Fatal("ftp accepted")
	}
}

func TestPinnedConfig(t *testing.T) {
	srv := hubtlstest.NewServer(t, ok, "127.0.0.1")
	fp, _ := hubtls.ParseFingerprint(hubtls.Fingerprint(srv.CA.Raw))

	cfg, pin := hubtls.PinnedConfig(fp, "127.0.0.1")
	if err := get(t, hubtls.Client(cfg, 5*time.Second), srv.URL); err != nil {
		t.Fatal(err)
	}
	if pin.Certificate() == nil || !pin.Certificate().Equal(srv.CA) {
		t.Fatal("pinned CA not recorded")
	}
	if string(pin.PEM()) != string(srv.CAPEM) {
		t.Fatal("PEM differs")
	}

	wrong, _ := hubtls.ParseFingerprint("sha256:" + strings.Repeat("00", 32))
	cfg, pin = hubtls.PinnedConfig(wrong, "127.0.0.1")
	err := get(t, hubtls.Client(cfg, 5*time.Second), srv.URL)
	if err == nil || !strings.Contains(err.Error(), "does not contain the CA") || pin.Certificate() != nil {
		t.Fatalf("wrong fingerprint: %v", err)
	}
}

func TestPinnedConfigChecksHostName(t *testing.T) {
	srv := hubtlstest.NewServer(t, ok, "other.example.lan")
	fp, _ := hubtls.ParseFingerprint(hubtls.Fingerprint(srv.CA.Raw))
	cfg, _ := hubtls.PinnedConfig(fp, "127.0.0.1")
	if cfg2, _ := hubtls.PinnedConfig(fp, ""); get(t, hubtls.Client(cfg2, 5*time.Second), srv.URL) == nil {
		t.Fatal("empty host accepted")
	}
	err := hubtls.Explain(get(t, hubtls.Client(cfg, 5*time.Second), srv.URL))
	if err == nil || !strings.Contains(err.Error(), "TOPCOAT_TLS_HOSTNAMES") {
		t.Fatalf("host name mismatch: %v", err)
	}
}

func TestRootsAndSystemConfig(t *testing.T) {
	srv := hubtlstest.NewServer(t, ok, "127.0.0.1")
	cfg, err := hubtls.RootsConfig(srv.CAPEM)
	if err != nil {
		t.Fatal(err)
	}
	if err := get(t, hubtls.Client(cfg, 5*time.Second), srv.URL); err != nil {
		t.Fatal(err)
	}
	err = hubtls.Explain(get(t, hubtls.Client(hubtls.SystemConfig(), 5*time.Second), srv.URL))
	if err == nil || !strings.Contains(err.Error(), "--ca-fingerprint") {
		t.Fatalf("system trust accepted a private CA: %v", err)
	}
	if _, err := hubtls.RootsConfig([]byte("not pem")); err == nil {
		t.Fatal("garbage CA accepted")
	}
}

func TestForIdentity(t *testing.T) {
	srv := hubtlstest.NewServer(t, ok, "127.0.0.1")
	d, err := store.Open(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	id := store.Identity{HubURL: srv.URL, NodeID: "n"}

	cfg, err := hubtls.ForIdentity(d, id)
	if err != nil || cfg == nil || cfg.RootCAs != nil {
		t.Fatalf("without hub-ca.pem want system trust: %v %+v", err, cfg)
	}
	if err := d.SaveHubCA(srv.CAPEM); err != nil {
		t.Fatal(err)
	}
	cfg, err = hubtls.ForIdentity(d, id)
	if err != nil {
		t.Fatal(err)
	}
	if err := get(t, hubtls.Client(cfg, 5*time.Second), srv.URL); err != nil {
		t.Fatalf("stored CA not used: %v", err)
	}

	if _, err := hubtls.ForIdentity(d, store.Identity{HubURL: "http://127.0.0.1:1", NodeID: "n"}); !errors.Is(err, hubtls.ErrInsecureHTTP) {
		t.Fatal(err)
	}
	cfg, err = hubtls.ForIdentity(d, store.Identity{HubURL: "http://127.0.0.1:1", NodeID: "n", InsecureHTTP: true})
	if err != nil || cfg != nil {
		t.Fatal(cfg, err)
	}
}
