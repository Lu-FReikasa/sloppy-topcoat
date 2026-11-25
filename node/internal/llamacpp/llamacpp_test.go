package llamacpp

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
)

type entry struct {
	name, body, link string
	mode             int64
}

func tgz(t *testing.T, entries []entry) []byte {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for _, e := range entries {
		h := &tar.Header{Name: e.name, Mode: e.mode, Size: int64(len(e.body)), Typeflag: tar.TypeReg}
		switch {
		case strings.HasSuffix(e.name, "/"):
			h.Typeflag, h.Size = tar.TypeDir, 0
		case e.link != "":
			h.Typeflag, h.Linkname, h.Size = tar.TypeSymlink, e.link, 0
		}
		if h.Mode == 0 {
			h.Mode = 0o644
		}
		if err := tw.WriteHeader(h); err != nil {
			t.Fatal(err)
		}
		if h.Typeflag == tar.TypeReg {
			_, _ = tw.Write([]byte(e.body))
		}
	}
	_ = tw.Close()
	_ = gz.Close()
	return buf.Bytes()
}

func zipOf(t *testing.T, entries []entry) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for _, e := range entries {
		w, err := zw.Create(e.name)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = w.Write([]byte(e.body))
	}
	_ = zw.Close()
	return buf.Bytes()
}

func sum(b []byte) string { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }

func serve(t *testing.T, files map[string][]byte) (*httptest.Server, *atomic.Int32) {
	t.Helper()
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		b, ok := files[r.URL.Path]
		if !ok {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write(b)
	}))
	t.Cleanup(srv.Close)
	return srv, &hits
}

func TestEmbeddedManifest(t *testing.T) {
	m := Pinned()
	for _, goos := range []string{"linux", "windows"} {
		for _, be := range []string{BackendCPU, BackendVulkan, BackendCUDA} {
			b, err := m.Build(m.Default, goos, be)
			if err != nil {
				t.Fatalf("%s/%s: %v", goos, be, err)
			}
			for _, a := range b.Assets {
				if !strings.Contains(a.Name, m.Default) && !strings.HasPrefix(a.Name, "cudart-") {
					t.Errorf("asset %s does not belong to %s", a.Name, m.Default)
				}
			}
		}
	}
	if _, err := m.Build("b1", "linux", "cpu"); err == nil {
		t.Fatal("unknown release accepted")
	}
}

func TestParseManifestRejectsBadEntries(t *testing.T) {
	for _, bad := range []string{
		`{"default":"b1","releases":{}}`,
		`{"default":"b1","releases":{"b1":{"builds":[{"os":"linux","backend":"cpu","assets":[{"name":"../x.zip","sha256":"` + strings.Repeat("a", 64) + `"}]}]}}}`,
		`{"default":"b1","releases":{"b1":{"builds":[{"os":"linux","backend":"cpu","assets":[{"name":"x.zip","sha256":"abc"}]}]}}}`,
		`{"default":"x1","releases":{"x1":{"builds":[]}}}`,
	} {
		if _, err := ParseManifest([]byte(bad)); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
}

func TestSelectBackend(t *testing.T) {
	cases := []struct {
		cfg     string
		vendors []string
		want    string
	}{
		{"auto", []string{"nvidia"}, BackendCUDA},
		{"auto", []string{"amd"}, BackendVulkan},
		{"auto", []string{"intel"}, BackendVulkan},
		{"auto", []string{"intel", "nvidia"}, BackendCUDA},
		{"auto", nil, BackendCPU},
		{"", []string{"other"}, BackendCPU},
		{"cpu", []string{"nvidia"}, BackendCPU},
		{"vulkan", []string{"nvidia"}, BackendVulkan},
	}
	for _, c := range cases {
		if got := SelectBackend(c.cfg, c.vendors); got != c.want {
			t.Errorf("%s %v: got %s want %s", c.cfg, c.vendors, got, c.want)
		}
	}
}

func testManifest(assets map[string]string) Manifest {
	var list []Asset
	for _, n := range []string{"llama-b1-bin-ubuntu-cuda-x64.tar.gz", "cudart-llama-b1-bin-ubuntu-cuda-x64.tar.gz", "llama-b1-bin-win-vulkan-x64.zip"} {
		if s, ok := assets[n]; ok {
			list = append(list, Asset{Name: n, SHA256: s})
		}
	}
	linux := []Asset{}
	win := []Asset{}
	for _, a := range list {
		if strings.HasSuffix(a.Name, ".zip") {
			win = append(win, a)
		} else {
			linux = append(linux, a)
		}
	}
	return Manifest{Default: "b1", Releases: map[string]Release{"b1": {Builds: []Build{
		{OS: "linux", Backend: BackendCUDA, Assets: linux},
		{OS: "windows", Backend: BackendVulkan, Assets: win},
	}}}}
}

func TestEnsureLinuxCUDAWithRuntimeLibs(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("unpacking symbolic links of a Linux archive needs a privilege on Windows")
	}
	main := tgz(t, []entry{
		{name: "llama-b1/"},
		{name: "llama-b1/llama-server", body: "server", mode: 0o755},
		{name: "llama-b1/llama-bench", body: "bench", mode: 0o755},
		{name: "llama-b1/libggml-cuda.so", body: "cuda"},
		{name: "llama-b1/libllama.so", link: "libllama.so.0"},
		{name: "llama-b1/libllama.so.0", body: "llama"},
	})
	cudart := tgz(t, []entry{
		{name: "./cudart-llama-b1-bin-ubuntu-cuda-x64/"},
		{name: "./cudart-llama-b1-bin-ubuntu-cuda-x64/libcudart.so.12", body: "rt"},
	})
	srv, hits := serve(t, map[string][]byte{
		"/b1/llama-b1-bin-ubuntu-cuda-x64.tar.gz":        main,
		"/b1/cudart-llama-b1-bin-ubuntu-cuda-x64.tar.gz": cudart,
	})
	root := t.TempDir()
	in := &Installer{Root: root, BaseURL: srv.URL, GOOS: "linux", Manifest: testManifest(map[string]string{
		"llama-b1-bin-ubuntu-cuda-x64.tar.gz":        sum(main),
		"cudart-llama-b1-bin-ubuntu-cuda-x64.tar.gz": sum(cudart),
	})}
	var last int64
	inst, err := in.Ensure(context.Background(), "b1", BackendCUDA, func(done, total int64) { last = done })
	if err != nil {
		t.Fatal(err)
	}
	if last != int64(len(main)+len(cudart)) {
		t.Fatalf("progress %d", last)
	}
	dir := filepath.Join(root, "b1-cuda")
	if inst.Dir != dir || inst.Server != filepath.Join(dir, "llama-server") || inst.Bench != filepath.Join(dir, "llama-bench") {
		t.Fatalf("%+v", inst)
	}
	if b, err := os.ReadFile(filepath.Join(dir, "libcudart.so.12")); err != nil || string(b) != "rt" {
		t.Fatalf("cuda runtime not next to the binaries: %v", err)
	}
	if l, err := os.Readlink(filepath.Join(dir, "libllama.so")); err != nil || l != "libllama.so.0" {
		t.Fatalf("symlink %q %v", l, err)
	}
	if st, _ := os.Stat(inst.Server); runtime.GOOS != "windows" && st.Mode().Perm()&0o100 == 0 {
		t.Fatalf("server not executable: %v", st.Mode())
	}
	// Second call: already installed, no download.
	n := hits.Load()
	if _, err := in.Ensure(context.Background(), "b1", BackendCUDA, nil); err != nil || hits.Load() != n {
		t.Fatalf("reinstalled: %v", err)
	}
	if _, ok := in.Installed("b1", BackendCUDA); !ok {
		t.Fatal("not reported as installed")
	}
}

func TestEnsureWindowsZipFlat(t *testing.T) {
	z := zipOf(t, []entry{{name: "llama-server.exe", body: "s"}, {name: "llama-bench.exe", body: "b"}, {name: "ggml-vulkan.dll", body: "v"}})
	srv, _ := serve(t, map[string][]byte{"/b1/llama-b1-bin-win-vulkan-x64.zip": z})
	root := t.TempDir()
	in := &Installer{Root: root, BaseURL: srv.URL, GOOS: "windows", Manifest: testManifest(map[string]string{
		"llama-b1-bin-win-vulkan-x64.zip": sum(z),
	})}
	inst, err := in.Ensure(context.Background(), "b1", BackendVulkan, nil)
	if err != nil {
		t.Fatal(err)
	}
	if filepath.Base(inst.Server) != "llama-server.exe" || filepath.Base(inst.Bench) != "llama-bench.exe" {
		t.Fatalf("%+v", inst)
	}
	if _, err := os.Stat(filepath.Join(inst.Dir, "ggml-vulkan.dll")); err != nil {
		t.Fatal(err)
	}
}

func TestEnsureRejectsChecksumMismatchAndTraversal(t *testing.T) {
	good := tgz(t, []entry{{name: "llama-server", body: "x"}, {name: "llama-bench", body: "y"}})
	evil := tgz(t, []entry{{name: "../evil", body: "x"}, {name: "llama-server", body: "x"}})
	srv, _ := serve(t, map[string][]byte{"/b1/llama-b1-bin-ubuntu-cuda-x64.tar.gz": good})
	root := t.TempDir()
	in := &Installer{Root: root, BaseURL: srv.URL, GOOS: "linux", Manifest: testManifest(map[string]string{
		"llama-b1-bin-ubuntu-cuda-x64.tar.gz": strings.Repeat("0", 64),
	})}
	_, err := in.Ensure(context.Background(), "b1", BackendCUDA, nil)
	if err == nil || !strings.Contains(err.Error(), "SHA-256 mismatch") {
		t.Fatalf("got %v", err)
	}
	if _, ok := in.Installed("b1", BackendCUDA); ok {
		t.Fatal("installed despite mismatch")
	}
	entries, _ := os.ReadDir(root)
	for _, e := range entries {
		t.Errorf("left behind: %s", e.Name())
	}

	srv2, _ := serve(t, map[string][]byte{"/b1/llama-b1-bin-ubuntu-cuda-x64.tar.gz": evil})
	in.BaseURL = srv2.URL
	in.Manifest = testManifest(map[string]string{"llama-b1-bin-ubuntu-cuda-x64.tar.gz": sum(evil)})
	if _, err := in.Ensure(context.Background(), "b1", BackendCUDA, nil); err == nil || !strings.Contains(err.Error(), "escapes") {
		t.Fatalf("traversal: %v", err)
	}
	if _, err := os.Stat(filepath.Join(filepath.Dir(root), "evil")); err == nil {
		t.Fatal("file written outside the root")
	}
}

func TestEnsureMissingBuildAndHTTPError(t *testing.T) {
	srv, _ := serve(t, map[string][]byte{})
	in := &Installer{Root: t.TempDir(), BaseURL: srv.URL, GOOS: "linux", Manifest: testManifest(map[string]string{
		"llama-b1-bin-ubuntu-cuda-x64.tar.gz": strings.Repeat("a", 64),
	})}
	if _, err := in.Ensure(context.Background(), "b1", BackendVulkan, nil); err == nil || !strings.Contains(err.Error(), "no official vulkan build") {
		t.Fatalf("got %v", err)
	}
	if _, err := in.Ensure(context.Background(), "b1", BackendCUDA, nil); err == nil || !strings.Contains(err.Error(), "HTTP 404") {
		t.Fatalf("got %v", err)
	}
}
