package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"topcoat/node/internal/update"
)

func do(t *testing.T, args ...string) (int, string, string) {
	t.Helper()
	var out, errb bytes.Buffer
	code := run(context.Background(), args, &out, &errb)
	return code, out.String(), errb.String()
}

// release makes a key and a signed build in a temp dir.
func release(t *testing.T) (dir, key, pub string) {
	t.Helper()
	dir = t.TempDir()
	key = filepath.Join(dir, "release.key")
	if code, _, e := do(t, "keygen", "--out", key); code != 0 {
		t.Fatalf("keygen: %s", e)
	}
	raw, err := os.ReadFile(key + ".pub")
	if err != nil {
		t.Fatal(err)
	}
	pub = strings.TrimSpace(string(raw))
	bin := filepath.Join(dir, "out", "topcoat-node")
	_ = os.MkdirAll(filepath.Dir(bin), 0o755)
	if err := os.WriteFile(bin, []byte("a program"), 0o755); err != nil { //nolint:gosec // test
		t.Fatal(err)
	}
	return dir, key, pub
}

func TestSignThenVerify(t *testing.T) {
	dir, key, pub := release(t)
	out := filepath.Join(dir, "out")
	code, stdout, stderr := do(t, "sign", "--key", key, "--binary", filepath.Join(out, "topcoat-node"),
		"--version", "0.2.0", "--os", "linux", "--arch", "amd64")
	if code != 0 {
		t.Fatalf("sign: %s", stderr)
	}
	if !strings.Contains(stdout, "signed topcoat-node 0.2.0") {
		t.Fatalf("output: %s", stdout)
	}
	if code, o, e := do(t, "verify", "--pub", pub, "--dir", out); code != 0 || !strings.Contains(o, "ok:") {
		t.Fatalf("verify: %d %s %s", code, o, e)
	}
	// A key file works as --pub too.
	if code, _, e := do(t, "verify", "--pub", key+".pub", "--dir", out); code != 0 {
		t.Fatalf("verify with a key file: %s", e)
	}
	// A changed program no longer verifies.
	if err := os.WriteFile(filepath.Join(out, "topcoat-node"), []byte("tampered!!"), 0o755); err != nil { //nolint:gosec // test
		t.Fatal(err)
	}
	if code, _, _ := do(t, "verify", "--pub", pub, "--dir", out); code == 0 {
		t.Fatal("a tampered program verified")
	}
}

func TestVerifyRejectsAnotherKey(t *testing.T) {
	dir, key, _ := release(t)
	out := filepath.Join(dir, "out")
	do(t, "sign", "--key", key, "--binary", filepath.Join(out, "topcoat-node"), "--version", "0.2.0", "--os", "linux", "--arch", "amd64")
	other := filepath.Join(t.TempDir(), "other.key")
	do(t, "keygen", "--out", other)
	if code, _, _ := do(t, "verify", "--pub", other+".pub", "--dir", out); code == 0 {
		t.Fatal("verified with the wrong key")
	}
}

func TestKeygenNeverOverwrites(t *testing.T) {
	dir, key, _ := release(t)
	_ = dir
	if code, _, _ := do(t, "keygen", "--out", key); code == 0 {
		t.Fatal("keygen replaced an existing key")
	}
}

func TestKeyFileMustBePrivate(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("no unix modes")
	}
	dir, key, _ := release(t)
	if err := os.Chmod(key, 0o644); err != nil { //nolint:gosec // test
		t.Fatal(err)
	}
	code, _, e := do(t, "sign", "--key", key, "--binary", filepath.Join(dir, "out", "topcoat-node"), "--version", "0.2.0", "--os", "linux", "--arch", "amd64")
	if code == 0 || !strings.Contains(e, "readable by others") {
		t.Fatalf("code %d, stderr %s", code, e)
	}
}

func TestSignRefusesBadInput(t *testing.T) {
	dir, key, _ := release(t)
	bin := filepath.Join(dir, "out", "topcoat-node")
	for name, args := range map[string][]string{
		"no version":     {"sign", "--key", key, "--binary", bin, "--os", "linux", "--arch", "amd64"},
		"bad version":    {"sign", "--key", key, "--binary", bin, "--version", "latest", "--os", "linux", "--arch", "amd64"},
		"bad channel":    {"sign", "--key", key, "--binary", bin, "--version", "1.0.0", "--os", "linux", "--arch", "amd64", "--channel", "../x"},
		"missing binary": {"sign", "--key", key, "--binary", bin + ".nope", "--version", "1.0.0", "--os", "linux", "--arch", "amd64"},
	} {
		if code, _, _ := do(t, args...); code == 0 {
			t.Errorf("%s: signed anyway", name)
		}
	}
}

func TestPublishUploadsManifestThenProgram(t *testing.T) {
	dir, key, pub := release(t)
	out := filepath.Join(dir, "out")
	var paths []string
	var tokens []string
	var gotManifest struct{ Manifest, Signature string }
	var gotBin []byte
	hub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.Method+" "+r.URL.Path)
		tokens = append(tokens, r.Header.Get("Authorization"))
		b, _ := io.ReadAll(r.Body)
		if strings.HasSuffix(r.URL.Path, "/manifest") {
			_ = json.Unmarshal(b, &gotManifest)
		} else {
			gotBin = b
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer hub.Close()
	t.Setenv(tokenEnv, "s3cret")
	code, stdout, stderr := do(t, "publish", "--key", key, "--binary", filepath.Join(out, "topcoat-node"),
		"--version", "0.2.0", "--os", "linux", "--arch", "amd64", "--channel", "test", "--hub", hub.URL, "--insecure-http")
	if code != 0 {
		t.Fatalf("publish: %s", stderr)
	}
	if strings.Contains(stdout+stderr, "s3cret") {
		t.Fatal("the token was printed")
	}
	want := []string{"PUT /api/updates/test/0.2.0/linux-amd64/manifest", "PUT /api/updates/test/0.2.0/linux-amd64/binary"}
	if strings.Join(paths, ",") != strings.Join(want, ",") {
		t.Fatalf("requests = %v", paths)
	}
	for _, tk := range tokens {
		if tk != "Bearer s3cret" {
			t.Fatalf("authorization = %q", tk)
		}
	}
	keys, _ := update.ParseKeys(pub)
	if err := update.Verify(keys, []byte(gotManifest.Manifest), gotManifest.Signature); err != nil {
		t.Fatalf("the uploaded manifest does not verify: %v", err)
	}
	if string(gotBin) != "a program" {
		t.Fatalf("uploaded program = %q", gotBin)
	}
}

func TestUploadNeedsTheTokenAndHttpsByDefault(t *testing.T) {
	dir, key, _ := release(t)
	out := filepath.Join(dir, "out")
	do(t, "sign", "--key", key, "--binary", filepath.Join(out, "topcoat-node"), "--version", "0.2.0", "--os", "linux", "--arch", "amd64")
	t.Setenv(tokenEnv, "")
	if code, _, e := do(t, "upload", "--dir", out, "--hub", "https://hub.example.lan:8443"); code == 0 || !strings.Contains(e, tokenEnv) {
		t.Fatalf("no token: %d %s", code, e)
	}
	t.Setenv(tokenEnv, "x")
	if code, _, e := do(t, "upload", "--dir", out, "--hub", "http://hub.example.lan:8443"); code == 0 || !strings.Contains(e, "plain http") {
		t.Fatalf("plain http: %d %s", code, e)
	}
}

func TestUploadReportsTheHubsAnswer(t *testing.T) {
	dir, key, _ := release(t)
	out := filepath.Join(dir, "out")
	do(t, "sign", "--key", key, "--binary", filepath.Join(out, "topcoat-node"), "--version", "0.2.0", "--os", "linux", "--arch", "amd64")
	hub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `{"code":"forbidden"}`, http.StatusForbidden)
	}))
	defer hub.Close()
	t.Setenv(tokenEnv, "x")
	code, _, e := do(t, "upload", "--dir", out, "--hub", hub.URL, "--insecure-http")
	if code == 0 || !strings.Contains(e, "403") {
		t.Fatalf("%d %s", code, e)
	}
}

func TestUsageAndUnknownCommand(t *testing.T) {
	if code, _, _ := do(t); code != 2 {
		t.Fatalf("no args: %d", code)
	}
	if code, _, _ := do(t, "nope"); code != 2 {
		t.Fatalf("unknown: %d", code)
	}
	if code, o, _ := do(t, "help"); code != 0 || !strings.Contains(o, "keygen") {
		t.Fatalf("help: %d", code)
	}
}
