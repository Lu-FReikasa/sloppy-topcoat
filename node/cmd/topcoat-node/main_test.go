package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/hubtls"
	"topcoat/node/internal/hubtls/hubtlstest"
	"topcoat/node/internal/llamacpp"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/gpu"
	"topcoat/node/internal/providers/runner"
	"topcoat/node/internal/service"
	"topcoat/node/internal/store"
)

// TestMain lets a test run the real main() in a child process: exit codes are what scripts and
// the Windows shell see, and only os.Exit in main() turns run()'s result into one.
func TestMain(m *testing.M) {
	if os.Getenv("TOPCOAT_NODE_TEST_MAIN") == "1" {
		main()
		return
	}
	os.Exit(m.Run())
}

// exitCode runs the test binary as topcoat-node with args and returns the process's exit code.
func exitCode(t *testing.T, args ...string) (int, string) {
	t.Helper()
	cmd := exec.Command(os.Args[0], args...) //nolint:gosec // the test binary itself
	cmd.Env = append(os.Environ(), "TOPCOAT_NODE_TEST_MAIN=1")
	out, err := cmd.CombinedOutput()
	if err == nil {
		return 0, string(out)
	}
	var ee *exec.ExitError
	if !errors.As(err, &ee) {
		t.Fatalf("run %v: %v", args, err)
	}
	return ee.ExitCode(), string(out)
}

func runCmd(ctx context.Context, args ...string) (int, string, string) {
	var out, errb bytes.Buffer
	code := run(ctx, args, &out, &errb)
	return code, out.String(), errb.String()
}

func TestVersionAndUsage(t *testing.T) {
	code, out, _ := runCmd(context.Background(), "version")
	if code != 0 || !strings.Contains(out, "0.1.0-dev") || !strings.Contains(out, "protocol v1") {
		t.Fatalf("%d %q", code, out)
	}
	if code, _, errOut := runCmd(context.Background()); code != 2 || !strings.Contains(errOut, "Usage") {
		t.Fatal("usage without args")
	}
	if code, _, _ := runCmd(context.Background(), "bogus"); code != 2 {
		t.Fatal("unknown command")
	}
	if code, out, _ := runCmd(context.Background(), "help"); code != 0 || !strings.Contains(out, "hub.example.lan") {
		t.Fatal("help")
	}
}

func TestPairFlags(t *testing.T) {
	if code, _, _ := runCmd(context.Background(), "pair", "--hub", "http://hub.example.lan:8000"); code != 2 {
		t.Fatal("missing --code accepted")
	}
	if code, _, _ := runCmd(context.Background(), "pair", "--nope"); code != 2 {
		t.Fatal("bad flag accepted")
	}
	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"node_id":"3f0c2a4e-8a51-4b7e-9a0e-1f2d3c4b5a60"}`))
	})
	srv := hubtlstest.NewServer(t, handler, "127.0.0.1")
	dir := filepath.Join(t.TempDir(), "d")
	code, out, errOut := runCmd(context.Background(), "pair", "--hub", srv.URL, "--code", "ABCD-1234", "--name", "node-test",
		"--data-dir", dir, "--ca-fingerprint", hubtls.Fingerprint(srv.CA.Raw))
	if code != 0 || !strings.Contains(out, "paired") {
		t.Fatalf("%d %s %s", code, out, errOut)
	}
	if _, err := os.Stat(filepath.Join(dir, store.HubCAFile)); err != nil {
		t.Fatal("hub CA not stored")
	}
	if code, _, errOut := runCmd(context.Background(), "pair", "--hub", srv.URL, "--code", "ABCD-1234", "--data-dir", dir); code != 1 || !strings.Contains(errOut, "already paired") {
		t.Fatalf("re-pair: %d %s", code, errOut)
	}
	plain := httptest.NewServer(handler)
	defer plain.Close()
	plainDir := filepath.Join(t.TempDir(), "p")
	if code, _, errOut := runCmd(context.Background(), "pair", "--hub", plain.URL, "--code", "ABCD-1234", "--name", "n", "--data-dir", plainDir); code != 1 || !strings.Contains(errOut, "plain http") {
		t.Fatalf("plain http without opt-in: %d %s", code, errOut)
	}
	if code, _, errOut := runCmd(context.Background(), "pair", "--hub", plain.URL, "--code", "ABCD-1234", "--name", "n", "--data-dir", plainDir, "--insecure-http"); code != 0 {
		t.Fatalf("plain http with opt-in: %d %s", code, errOut)
	}
}

func TestRunRequiresPairing(t *testing.T) {
	dir := t.TempDir()
	if code, _, errOut := runCmd(context.Background(), "run", "--data-dir", dir, "--fake"); code != 1 || !strings.Contains(errOut, "not paired") {
		t.Fatalf("%d %s", code, errOut)
	}
	if code, _, _ := runCmd(context.Background(), "run", "--log-level", "loud"); code != 2 {
		t.Fatal("bad log level")
	}
	if code, _, _ := runCmd(context.Background(), "run", "--data-dir", dir, "--fake-profile", "x.json"); code != 2 {
		t.Fatal("--fake-profile without --fake")
	}
	if code, _, _ := runCmd(context.Background(), "run", "--data-dir", dir, "--fake-control", "x.json"); code != 2 {
		t.Fatal("--fake-control without --fake")
	}
}

func TestRunFakeUntilCancelled(t *testing.T) {
	dir := t.TempDir()
	d, _ := store.Open(dir)
	// Hub that is down: the agent keeps retrying until canceled.
	srv := httptest.NewServer(http.NotFoundHandler())
	url := srv.URL
	srv.Close()
	savePaired(t, d, url)
	profile := filepath.Join("..", "..", "fake-profile.example.json")
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	code, _, errOut := runCmd(ctx, "run", "--data-dir", dir, "--fake", "--fake-profile", profile, "--log-level", "debug")
	if code != 0 || !strings.Contains(errOut, "FAKE providers") || !strings.Contains(errOut, "agent stopped") {
		t.Fatalf("%d\n%s", code, errOut)
	}
	if code, _, _ := runCmd(context.Background(), "run", "--data-dir", dir, "--fake", "--fake-profile", "missing.json"); code != 1 {
		t.Fatal("missing profile accepted")
	}
}

func TestRealRunnerSelection(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	nvidia := staticGPUs{{Vendor: "nvidia", Name: "RTX"}}
	dir := t.TempDir()
	orig := newInstaller
	newInstaller = func(root string) *llamacpp.Installer {
		in := orig(root)
		in.BaseURL = "http://127.0.0.1:1" // never download in tests
		return in
	}
	defer func() { newInstaller = orig }()
	factory := realRunner(dir, nvidia, nil, logger)
	for _, typ := range []string{"unknown", "fake"} {
		r := factory(protocol.RunnerConfig{Type: typ})
		if r.Type() != typ || r.Health(context.Background()) == nil {
			t.Errorf("%s: %s healthy=%v", typ, r.Type(), r.Health(context.Background()) == nil)
		}
	}
	r := factory(protocol.RunnerConfig{Type: "llamaserver", Backend: "auto"})
	ls, ok := r.(*runner.LlamaServer)
	if !ok {
		t.Fatalf("got %T", r)
	}
	defer ls.Close(context.Background())
	if ri := ls.RuntimeInfo(); ri.Backend != "cuda" || ri.Release != llamacpp.Pinned().Default {
		t.Fatalf("%+v", ri)
	}
	// Unknown pinned release -> unavailable with the reason.
	r = factory(protocol.RunnerConfig{Type: "llamaserver", LlamaCppRelease: "b1"})
	if err := r.Health(context.Background()); err == nil || !strings.Contains(err.Error(), "not pinned") {
		t.Fatalf("got %v", err)
	}
}

type staticGPUs []gpu.GPU

func (s staticGPUs) GPUs(context.Context) ([]gpu.GPU, error) { return s, nil }

func TestInstallFlags(t *testing.T) {
	if code, _, _ := runCmd(context.Background(), "install", "--nope"); code != 2 {
		t.Fatal("bad flag accepted")
	}
	if code, _, _ := runCmd(context.Background(), "uninstall", "--nope"); code != 2 {
		t.Fatal("bad flag accepted")
	}
	// Never really install a service from tests (CI runners are admin/root).
	if runtime.GOOS == "linux" && os.Geteuid() != 0 {
		if code, _, errOut := runCmd(context.Background(), "install", "--hub", "http://hub.example.lan:8000", "--code", "ABCD-1234"); code != 1 || !strings.Contains(errOut, "sudo") {
			t.Fatalf("%d %s", code, errOut)
		}
	}
}

func TestLogFile(t *testing.T) {
	dir := t.TempDir()
	d, _ := store.Open(dir)
	savePaired(t, d, "http://127.0.0.1:1")
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	if code, _, errOut := runCmd(ctx, "run", "--data-dir", dir, "--fake", "--log-file", "auto"); code != 0 {
		t.Fatalf("%d %s", code, errOut)
	}
	b, err := os.ReadFile(filepath.Join(dir, "logs", "topcoat-node.log"))
	if err != nil || !strings.Contains(string(b), "topcoat-node starting") {
		t.Fatalf("log file: %v %s", err, b)
	}
}

// savePaired writes what `pair --insecure-http` would leave in the data dir.
func savePaired(t *testing.T, d *store.Dir, hubURL string) {
	t.Helper()
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	if err := d.SavePrivateKey(key); err != nil {
		t.Fatal(err)
	}
	id := store.Identity{HubURL: hubURL, NodeID: "3f0c2a4e-8a51-4b7e-9a0e-1f2d3c4b5a60", InsecureHTTP: true}
	if err := d.SaveIdentity(id); err != nil {
		t.Fatal(err)
	}
}

func TestRunRefusesPlainHTTPWithoutOptIn(t *testing.T) {
	dir := t.TempDir()
	d, _ := store.Open(dir)
	savePaired(t, d, "http://127.0.0.1:1")
	id, _ := d.LoadIdentity()
	id.InsecureHTTP = false
	if err := d.SaveIdentity(id); err != nil {
		t.Fatal(err)
	}
	code, _, errOut := runCmd(context.Background(), "run", "--data-dir", dir, "--fake")
	if code != 1 || !strings.Contains(errOut, "plain http") {
		t.Fatalf("%d %s", code, errOut)
	}
}

func TestSetHubMovesTheNodeAndRestartsTheService(t *testing.T) {
	health := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"status":"ok","version":"0.1.0"}`))
	})
	srv := hubtlstest.NewServer(t, health, "localhost", "127.0.0.1")
	dir := t.TempDir()
	d, _ := store.Open(dir)
	if err := d.SaveIdentity(store.Identity{HubURL: "https://old.example.lan:8443", NodeID: "node-1"}); err != nil {
		t.Fatal(err)
	}
	if err := d.SaveHubCA(srv.CAPEM); err != nil {
		t.Fatal(err)
	}
	restarts := 0
	old := restartService
	defer func() { restartService = old }()
	restartService = func(service.RestartOptions) error { restarts++; return nil }

	if code, _, _ := runCmd(context.Background(), "set-hub", "--data-dir", dir); code != 2 {
		t.Fatal("--hub is required")
	}
	newURL := strings.Replace(srv.URL, "127.0.0.1", "localhost", 1)
	code, out, errOut := runCmd(context.Background(), "set-hub", "--hub", newURL, "--data-dir", dir)
	if code != 0 || restarts != 1 || !strings.Contains(out, "old.example.lan:8443 -> "+newURL) || !strings.Contains(out, "pinned") {
		t.Fatalf("%d restarts=%d\n%s\n%s", code, restarts, out, errOut)
	}
	if id, _ := d.LoadIdentity(); id.HubURL != newURL || id.NodeID != "node-1" {
		t.Fatalf("%+v", id)
	}

	// without a service: the address is saved and the user is told to restart
	restartService = func(service.RestartOptions) error { return service.ErrNotInstalled }
	if code, out, _ := runCmd(context.Background(), "set-hub", "--hub", newURL, "--data-dir", dir); code != 0 || !strings.Contains(out, "restart `topcoat-node run` yourself") {
		t.Fatalf("%d %s", code, out)
	}
	// a restart that fails is an error, but the address stays saved
	restartService = func(service.RestartOptions) error { return errors.New("access denied") }
	if code, _, errOut := runCmd(context.Background(), "set-hub", "--hub", newURL, "--data-dir", dir); code != 1 || !strings.Contains(errOut, "address is saved") {
		t.Fatalf("%d %s", code, errOut)
	}
	// --no-restart never touches the service
	restarts = 0
	if code, _, _ := runCmd(context.Background(), "set-hub", "--hub", newURL, "--data-dir", dir, "--no-restart"); code != 0 || restarts != 0 {
		t.Fatalf("no-restart: %d %d", code, restarts)
	}
	// a hub with another CA is refused and nothing changes
	other := hubtlstest.NewServer(t, health, "localhost")
	if code, _, errOut := runCmd(context.Background(), "set-hub", "--hub", strings.Replace(other.URL, "127.0.0.1", "localhost", 1), "--data-dir", dir); code != 1 || !strings.Contains(errOut, "pinned") {
		t.Fatalf("%d %s", code, errOut)
	}
	if id, _ := d.LoadIdentity(); id.HubURL != newURL {
		t.Fatalf("a refused move changed node.json: %+v", id)
	}
}

// A refused set-hub must end the process with a non-zero code (a script — or a cmd.exe user
// looking at %ERRORLEVEL% — has nothing else to go by), a successful one with 0.
func TestSetHubExitCodesOfTheRealProcess(t *testing.T) {
	health := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"status":"ok","version":"0.1.0"}`))
	})
	srv := hubtlstest.NewServer(t, health, "localhost", "127.0.0.1")
	other := hubtlstest.NewServer(t, health, "localhost")
	dir := t.TempDir()
	d, _ := store.Open(dir)
	if err := d.SaveIdentity(store.Identity{HubURL: "https://old.example.lan:8443", NodeID: "node-1"}); err != nil {
		t.Fatal(err)
	}
	if err := d.SaveHubCA(srv.CAPEM); err != nil {
		t.Fatal(err)
	}
	local := func(u string) string { return strings.Replace(u, "127.0.0.1", "localhost", 1) }

	for name, args := range map[string][]string{
		"wrong address":     {"set-hub", "--hub", "https://127.0.0.1:1", "--data-dir", dir, "--no-restart"},
		"another CA":        {"set-hub", "--hub", local(other.URL), "--data-dir", dir, "--no-restart"},
		"not a URL":         {"set-hub", "--hub", "hub.example.lan", "--data-dir", dir, "--no-restart"},
		"no address at all": {"set-hub", "--data-dir", dir},
	} {
		code, out := exitCode(t, args...)
		if code == 0 {
			t.Errorf("%s: exit code 0, output: %s", name, out)
		}
		if id, _ := d.LoadIdentity(); id.HubURL != "https://old.example.lan:8443" {
			t.Fatalf("%s changed node.json: %+v", name, id)
		}
	}
	if code, out := exitCode(t, "set-hub", "--hub", local(srv.URL), "--data-dir", dir, "--no-restart"); code != 0 {
		t.Fatalf("a good move: exit code %d\n%s", code, out)
	}
}

func TestTheInputHelperIsWindowsOnly(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("on Windows it would start serving")
	}
	code, out, errOut := runCmd(context.Background(), "input-helper")
	if code != 1 || !strings.Contains(errOut, "Windows only") {
		t.Fatalf("%d %s %s", code, out, errOut)
	}
}

func TestRoleMustBeGpuToolsOrDev(t *testing.T) {
	for _, cmd := range []string{"install", "pair"} {
		code, _, errOut := runCmd(context.Background(), cmd, "--hub", "https://hub.example.lan", "--code", "ABCD-1234", "--role", "admin")
		if code != 2 || !strings.Contains(errOut, "--role must be gpu, tools or dev") {
			t.Errorf("%s: %d %q", cmd, code, errOut)
		}
	}
}
