package runner

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"topcoat/node/internal/llamacpp"
	"topcoat/node/internal/proc"
	"topcoat/node/internal/sdcpp"
)

// sdStarter imitates sd-server: it listens on --listen-port and answers /v1/models.
type sdStarter struct {
	mu      sync.Mutex
	started []*fakeServer
	dies    string // non-empty: the server prints this and exits at once (e.g. a missing glibc)
}

func (s *sdStarter) start(_ context.Context, bin string, args []string, _ string, out io.Writer) (proc.Process, error) {
	port, _ := argOf(args, "--listen-port")
	f := &fakeServer{args: append([]string{bin}, args...), done: make(chan struct{})}
	ln, err := net.Listen("tcp", "127.0.0.1:"+port)
	if err != nil {
		return nil, err
	}
	f.srv = &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = w.Write([]byte(`{"data":[]}`))
	})}
	go func() { _ = f.srv.Serve(ln) }()
	if s.dies != "" {
		_, _ = out.Write([]byte(s.dies + "\n"))
		f.exit(errors.New("exit status 127"))
	}
	s.mu.Lock()
	s.started = append(s.started, f)
	s.mu.Unlock()
	return f, nil
}

func sdInstalled(t *testing.T, data string) *llamacpp.Installer {
	t.Helper()
	in := sdcpp.NewInstaller(filepath.Join(data, "runtime-sd"))
	rel := in.Manifest.Default
	dir := filepath.Join(in.Root, rel+"-vulkan")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	server := filepath.Join(dir, "sd-server")
	_ = os.WriteFile(server, []byte("#!"), 0o755)
	marker := fmt.Sprintf(`{"release":%q,"backend":"vulkan","dir":%q,"server":%q}`, rel, dir, server)
	if err := os.WriteFile(filepath.Join(dir, "topcoat-install.json"), []byte(marker), 0o644); err != nil {
		t.Fatal(err)
	}
	return in
}

func newSD(t *testing.T, st *sdStarter) (*SDServer, string) {
	t.Helper()
	data := t.TempDir()
	in := sdInstalled(t, data)
	s := NewSDServer(SDServerOptions{
		DataDir: data, Backend: "vulkan", Release: in.Manifest.Default, Threads: 4, Installer: in,
		Start: st.start, PollInterval: 5 * time.Millisecond, ReadyTimeout: 3 * time.Second,
	})
	t.Cleanup(func() { s.Close(context.Background()) })
	return s, data
}

func TestImageModelsAreGGUFAndSafetensorsButNeverPickles(t *testing.T) {
	s, data := newSD(t, &sdStarter{})
	dir := filepath.Join(data, "image-models")
	writeModel(t, dir, "sd-turbo-q8.gguf", 10)
	writeModel(t, filepath.Join(dir, "repo"), "flux-schnell.safetensors", 20)
	writeModel(t, dir, "old.ckpt", 5) // pickle: loading it can run code
	writeModel(t, dir, "weights.pt", 5)
	models, err := s.ListImageModels(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	got := []string{}
	for _, m := range models {
		got = append(got, m.Name+":"+m.Format)
	}
	if !slices.Equal(got, []string{"flux-schnell:safetensors", "sd-turbo-q8:gguf"}) {
		t.Fatalf("%v", got)
	}
	if !s.Info().Installed {
		t.Fatal("the installed build is reported")
	}
}

func TestAnImageModelRunsOnLoopbackOneAtATime(t *testing.T) {
	st := &sdStarter{}
	s, data := newSD(t, st)
	dir := filepath.Join(data, "image-models")
	writeModel(t, dir, "a.gguf", 10)
	writeModel(t, dir, "b.gguf", 10)
	ctx := context.Background()

	lm, err := s.LoadImage(ctx, "a")
	if err != nil {
		t.Fatal(err)
	}
	args := st.started[0].args
	if ip, _ := argOf(args, "--listen-ip"); ip != "127.0.0.1" {
		t.Fatalf("sd-server must listen on loopback only: %v", args)
	}
	if m, _ := argOf(args, "-m"); m != filepath.Join(dir, "a.gguf") {
		t.Fatalf("model %q", m)
	}
	if p, _ := argOf(args, "--listen-port"); p == "" || p != fmt.Sprint(lm.Port) {
		t.Fatalf("port %q vs %d", p, lm.Port)
	}
	if again, _ := s.LoadImage(ctx, "a"); again != lm || len(st.started) != 1 {
		t.Fatal("loading the running model again starts nothing")
	}

	if _, err := s.LoadImage(ctx, "b"); err != nil {
		t.Fatal(err)
	}
	if !st.started[0].stopped.Load() {
		t.Fatal("the first server must stop before the second model runs")
	}
	loaded, _ := s.LoadedImages(ctx)
	if len(loaded) != 1 || loaded[0].Name != "b" {
		t.Fatalf("%v", loaded)
	}
	if err := s.UnloadImage(ctx, "b"); err != nil {
		t.Fatal(err)
	}
	if loaded, _ := s.LoadedImages(ctx); len(loaded) != 0 {
		t.Fatalf("%v", loaded)
	}
	if err := s.UnloadImage(ctx, "b"); !errors.Is(err, ErrNotLoaded) {
		t.Fatalf("%v", err)
	}
	if _, err := s.LoadImage(ctx, "missing"); !errors.Is(err, ErrUnknownModel) {
		t.Fatalf("%v", err)
	}
}

func TestParallelLoadsLeaveNoOrphanedServer(t *testing.T) {
	st := &sdStarter{}
	s, data := newSD(t, st)
	dir := filepath.Join(data, "image-models")
	writeModel(t, dir, "a.gguf", 10)
	writeModel(t, dir, "b.gguf", 10)
	var wg sync.WaitGroup
	for i := range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _ = s.LoadImage(context.Background(), []string{"a", "b"}[i%2])
		}()
	}
	wg.Wait()
	st.mu.Lock()
	defer st.mu.Unlock()
	alive := 0
	for _, f := range st.started {
		if !f.stopped.Load() {
			alive++
		}
	}
	if alive != 1 {
		t.Fatalf("%d of %d started servers still run; exactly the recorded one may", alive, len(st.started))
	}
}

func TestAServerThatDiesAtStartSaysWhy(t *testing.T) {
	st := &sdStarter{dies: "sd-server: version `GLIBC_2.43' not found"}
	s, data := newSD(t, st)
	writeModel(t, filepath.Join(data, "image-models"), "a.gguf", 10)
	_, err := s.LoadImage(context.Background(), "a")
	if err == nil || !strings.Contains(err.Error(), "GLIBC_2.43") {
		t.Fatalf("the reason from the server's output belongs in the error: %v", err)
	}
	if loaded, _ := s.LoadedImages(context.Background()); len(loaded) != 0 {
		t.Fatalf("%v", loaded)
	}
}
