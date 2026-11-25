package sethub

import (
	"context"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"topcoat/node/internal/hubtls"
	"topcoat/node/internal/hubtls/hubtlstest"
	"topcoat/node/internal/store"
)

func hubHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"status":"ok","version":"0.1.0"}`))
	})
	return mux
}

// pairedDir is a data dir of a node paired with oldURL that pinned ca.
func pairedDir(t *testing.T, oldURL string, ca []byte) *store.Dir {
	t.Helper()
	d, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if err := d.SaveIdentity(store.Identity{HubURL: oldURL, NodeID: "node-1"}); err != nil {
		t.Fatal(err)
	}
	if ca != nil {
		if err := d.SaveHubCA(ca); err != nil {
			t.Fatal(err)
		}
	}
	return d
}

func portOf(t *testing.T, raw string) string {
	t.Helper()
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u.Port()
}

func TestMovesToAHubWithTheSamePinnedCA(t *testing.T) {
	srv := hubtlstest.NewServer(t, hubHandler(), "hub.example.lan", "localhost", "127.0.0.1")
	d := pairedDir(t, "https://old.example.lan:8443", srv.CAPEM)
	res, err := Move(context.Background(), Options{Dir: d, HubURL: "https://localhost:" + portOf(t, srv.URL) + "/"})
	if err != nil {
		t.Fatal(err)
	}
	id, err := d.LoadIdentity()
	if err != nil {
		t.Fatal(err)
	}
	if id.HubURL != "https://localhost:"+portOf(t, srv.URL) || id.NodeID != "node-1" || id.InsecureHTTP {
		t.Fatalf("identity %+v", id)
	}
	if res.Old.HubURL != "https://old.example.lan:8443" || !strings.HasPrefix(res.CA, "sha256:") {
		t.Fatalf("%+v", res)
	}
	if ca, ok, _ := d.LoadHubCA(); !ok || string(ca) != string(srv.CAPEM) {
		t.Fatal("the pinned CA must stay as it is")
	}
	if _, err := hubtls.ParseFingerprint(res.CA); err != nil {
		t.Fatal(err)
	}
}

func TestRefusesAHubWithAnotherCA(t *testing.T) {
	pinned := hubtlstest.NewServer(t, hubHandler(), "localhost")
	other := hubtlstest.NewServer(t, hubHandler(), "localhost")
	d := pairedDir(t, "https://old.example.lan:8443", pinned.CAPEM)
	_, err := Move(context.Background(), Options{Dir: d, HubURL: "https://localhost:" + portOf(t, other.URL)})
	if err == nil || !strings.Contains(err.Error(), "not signed by the CA this node pinned") {
		t.Fatalf("got %v", err)
	}
	if id, _ := d.LoadIdentity(); id.HubURL != "https://old.example.lan:8443" {
		t.Fatalf("a refused move must not touch node.json: %+v", id)
	}
}

func TestRefusesAServerThatIsNoHub(t *testing.T) {
	srv := hubtlstest.NewServer(t, http.NotFoundHandler(), "localhost")
	d := pairedDir(t, "https://old.example.lan:8443", srv.CAPEM)
	_, err := Move(context.Background(), Options{Dir: d, HubURL: "https://localhost:" + portOf(t, srv.URL)})
	if err == nil || !strings.Contains(err.Error(), "does not answer like a topcoat hub") {
		t.Fatalf("got %v", err)
	}
}

func TestRefusesBadInput(t *testing.T) {
	srv := hubtlstest.NewServer(t, hubHandler(), "localhost")
	d := pairedDir(t, "https://old.example.lan:8443", srv.CAPEM)
	for _, c := range []struct{ url, want string }{
		{"http://localhost:1", "plain http"},
		{"https://localhost:1/api", "no path"},
		{"", "must look like"},
		{"ftp://x", "scheme"},
	} {
		if _, err := Move(context.Background(), Options{Dir: d, HubURL: c.url}); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%q: got %v, want %q", c.url, err, c.want)
		}
	}
	empty, _ := store.Open(t.TempDir())
	if _, err := Move(context.Background(), Options{Dir: empty, HubURL: srv.URL}); err == nil || !strings.Contains(err.Error(), "not paired") {
		t.Errorf("unpaired: %v", err)
	}
}

func TestPlainHTTPNeedsTheOptIn(t *testing.T) {
	plain := httptestHub(t)
	d := pairedDir(t, "http://old.example.lan:8000", nil)
	if _, err := Move(context.Background(), Options{Dir: d, HubURL: plain, InsecureHTTP: true}); err != nil {
		t.Fatal(err)
	}
	if id, _ := d.LoadIdentity(); id.HubURL != plain || !id.InsecureHTTP {
		t.Fatalf("%+v", id)
	}
}

func TestIdentityFileStaysPrivate(t *testing.T) {
	srv := hubtlstest.NewServer(t, hubHandler(), "localhost")
	d := pairedDir(t, "https://old.example.lan:8443", srv.CAPEM)
	if _, err := Move(context.Background(), Options{Dir: d, HubURL: "https://localhost:" + portOf(t, srv.URL)}); err != nil {
		t.Fatal(err)
	}
	fi, err := os.Stat(filepath.Join(d.Path, store.IdentityFile))
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm()&0o077 != 0 && !isWindows() {
		t.Fatalf("node.json mode %v", fi.Mode())
	}
}
