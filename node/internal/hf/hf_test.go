package hf

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

type fakeHub struct {
	content  []byte
	sha      string
	cutAt    int  // first GET stops after this many bytes (0 = no cut)
	noRange  bool // ignore Range headers
	gets     int
	ranges   []string
	noLFS    bool
	metaCode int
}

func (f *fakeHub) handler(t *testing.T) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/models/org/repo/paths-info/main", func(w http.ResponseWriter, r *http.Request) {
		if f.metaCode != 0 {
			w.WriteHeader(f.metaCode)
			return
		}
		_ = r.ParseForm()
		p := r.PostForm.Get("paths")
		lfs := fmt.Sprintf(`,"lfs":{"oid":%q,"size":%d,"pointerSize":134}`, f.sha, len(f.content))
		if f.noLFS {
			lfs = ""
		}
		fmt.Fprintf(w, `[{"type":"file","oid":"abc","size":%d,"path":%q%s}]`, len(f.content), p, lfs)
	})
	mux.HandleFunc("GET /org/repo/resolve/main/sub/model.gguf", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/cdn/model.gguf", http.StatusFound)
	})
	mux.HandleFunc("GET /cdn/model.gguf", func(w http.ResponseWriter, r *http.Request) {
		f.gets++
		f.ranges = append(f.ranges, r.Header.Get("Range"))
		start := 0
		if rg := r.Header.Get("Range"); rg != "" && !f.noRange {
			start, _ = strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(rg, "bytes="), "-"))
			w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, len(f.content)-1, len(f.content)))
			w.WriteHeader(http.StatusPartialContent)
		}
		body := f.content[start:]
		if f.gets == 1 && f.cutAt > 0 {
			w.Header().Set("Content-Length", strconv.Itoa(len(body)))
			_, _ = w.Write(body[:f.cutAt])
			// Abort the connection mid-body.
			if hj, ok := w.(http.Hijacker); ok {
				c, _, _ := hj.Hijack()
				_ = c.Close()
			}
			return
		}
		_, _ = w.Write(body)
	})
	return mux
}

func newHub(t *testing.T, content []byte) (*fakeHub, *Client) {
	t.Helper()
	s := sha256.Sum256(content)
	f := &fakeHub{content: content, sha: hex.EncodeToString(s[:])}
	srv := httptest.NewServer(f.handler(t))
	t.Cleanup(srv.Close)
	return f, &Client{BaseURL: srv.URL, HTTP: srv.Client()}
}

var src = Source{Repo: "org/repo", File: "sub/model.gguf"}

func TestDownloadResumesAndVerifies(t *testing.T) {
	content := []byte(strings.Repeat("gguf-data-", 50000))
	f, c := newHub(t, content)
	f.cutAt = 12345
	dest := filepath.Join(t.TempDir(), "m.gguf")

	_, err := c.Download(context.Background(), src, dest, nil)
	if err == nil {
		t.Fatal("expected interrupted download to fail")
	}
	if st, err := os.Stat(dest + ".part"); err != nil || st.Size() != 12345 {
		t.Fatalf("part file: %v", err)
	}
	var last [2]int64
	info, err := c.Download(context.Background(), src, dest, func(d, total int64) { last = [2]int64{d, total} })
	if err != nil {
		t.Fatal(err)
	}
	if f.ranges[1] != "bytes=12345-" {
		t.Fatalf("ranges %v", f.ranges)
	}
	if info.Size != int64(len(content)) || last != [2]int64{info.Size, info.Size} {
		t.Fatalf("info %+v progress %v", info, last)
	}
	b, _ := os.ReadFile(dest)
	if string(b) != string(content) {
		t.Fatal("content differs")
	}
	if _, err := os.Stat(dest + ".part"); !os.IsNotExist(err) {
		t.Fatal("part file left")
	}
}

func TestDownloadRestartsWhenRangeIgnored(t *testing.T) {
	content := []byte(strings.Repeat("x", 100000))
	f, c := newHub(t, content)
	f.noRange = true
	dest := filepath.Join(t.TempDir(), "m.gguf")
	if err := os.WriteFile(dest+".part", content[:500], 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Download(context.Background(), src, dest, nil); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(dest)
	if len(b) != len(content) {
		t.Fatalf("size %d", len(b))
	}
}

func TestDownloadChecksumMismatch(t *testing.T) {
	f, c := newHub(t, []byte("real content"))
	f.sha = strings.Repeat("0", 64)
	dest := filepath.Join(t.TempDir(), "m.gguf")
	_, err := c.Download(context.Background(), src, dest, nil)
	if !errors.Is(err, ErrChecksum) {
		t.Fatalf("got %v", err)
	}
	for _, p := range []string{dest, dest + ".part"} {
		if _, err := os.Stat(p); !os.IsNotExist(err) {
			t.Fatalf("%s exists", p)
		}
	}
}

func TestDownloadRefusesUnverifiable(t *testing.T) {
	f, c := newHub(t, []byte("x"))
	f.noLFS = true
	if _, err := c.Download(context.Background(), src, filepath.Join(t.TempDir(), "m"), nil); err == nil || !strings.Contains(err.Error(), "no SHA-256") {
		t.Fatalf("got %v", err)
	}
	f.noLFS = false
	f.metaCode = 401
	if _, err := c.Download(context.Background(), src, filepath.Join(t.TempDir(), "m"), nil); err == nil || !strings.Contains(err.Error(), "HTTP 401") {
		t.Fatalf("got %v", err)
	}
}

func TestSourceValidate(t *testing.T) {
	ok := []Source{
		{Repo: "Qwen/Qwen2.5-1.5B-Instruct-GGUF", File: "qwen2.5-1.5b-instruct-q4_k_m.gguf"},
		{Repo: "a/b", File: "Q4_K_M/model.gguf", Revision: "v1.0"},
	}
	for _, s := range ok {
		if err := s.Validate(); err != nil {
			t.Errorf("%+v: %v", s, err)
		}
	}
	bad := []Source{
		{Repo: "nope", File: "a.gguf"},
		{Repo: "a/b", File: "../a.gguf"},
		{Repo: "a/b", File: "a.bin"},
		{Repo: "a/b", File: "/abs.gguf"},
		{Repo: "a/b", File: "m-00001-of-00002.gguf"},
		{Repo: "a/b", File: "a.gguf", Revision: "a b"},
		{Repo: "a/../b", File: "a.gguf"},
	}
	for _, s := range bad {
		if err := s.Validate(); err == nil {
			t.Errorf("accepted %+v", s)
		}
	}
}

func TestThrottle(t *testing.T) {
	now := time.Unix(0, 0)
	var calls []int64
	p := Throttle(func(d, _ int64) { calls = append(calls, d) }, 2*time.Second, func() time.Time { return now })
	p(1, 10)
	p(2, 10)
	now = now.Add(3 * time.Second)
	p(3, 10)
	p(10, 10)
	if fmt.Sprint(calls) != "[1 3 10]" {
		t.Fatalf("%v", calls)
	}
}

// gitHub serves one small file kept in git (no LFS entry): its oid is the git blob id.
func gitHub(t *testing.T, content []byte, oid string) *Client {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/models/org/repo/paths-info/main", func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		fmt.Fprintf(w, `[{"type":"file","oid":%q,"size":%d,"path":%q}]`, oid, len(content), r.PostForm.Get("paths"))
	})
	mux.HandleFunc("GET /org/repo/resolve/main/v.onnx.json", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(content)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return &Client{BaseURL: srv.URL, HTTP: srv.Client()}
}

func TestDownloadGitBlobOnlyWhenAllowed(t *testing.T) {
	content := []byte(`{"phoneme_id_map":{}}`)
	oid := "146f85f70a8f13261cde9c6f2412abbd36b3dfb8" // printf '%s' <content> | git hash-object --stdin
	c := gitHub(t, content, oid)
	s := Source{Repo: "org/repo", File: "v.onnx.json"}
	dest := filepath.Join(t.TempDir(), "v.onnx.json")
	if _, err := c.Download(context.Background(), s, dest, nil); err == nil || !strings.Contains(err.Error(), "no SHA-256") {
		t.Fatalf("without AllowGitBlob: %v", err)
	}
	s.AllowGitBlob = true
	info, err := c.Download(context.Background(), s, dest, nil)
	if err != nil || info.GitSHA1 != oid {
		t.Fatalf("%+v %v", info, err)
	}
	if b, _ := os.ReadFile(dest); string(b) != string(content) {
		t.Fatalf("%q", b)
	}
	// a wrong blob id fails the check and leaves no file
	c = gitHub(t, content, strings.Repeat("0", 40))
	dest2 := filepath.Join(t.TempDir(), "v.onnx.json")
	if _, err := c.Download(context.Background(), s, dest2, nil); !errors.Is(err, ErrChecksum) {
		t.Fatalf("got %v", err)
	}
	if _, err := os.Stat(dest2 + ".part"); !os.IsNotExist(err) {
		t.Fatal("partial file kept")
	}
}

func TestSourceValidateFile(t *testing.T) {
	ok := Source{Repo: "rhasspy/piper-voices", File: "de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx", Revision: "a60bfb135881"}
	if err := ok.ValidateFile(".onnx"); err != nil {
		t.Fatal(err)
	}
	for _, s := range []Source{
		{Repo: "a/b", File: "ggml-base.bin"},  // wrong extension for .onnx
		{Repo: "a/b", File: "x/../ggml.onnx"}, // escapes
		{Repo: "a/b", File: ".onnx"},          // extension only
		{Repo: "a/b", File: "v.onnx", Revision: "a/b"},
	} {
		if err := s.ValidateFile(".onnx"); err == nil {
			t.Errorf("accepted %+v", s)
		}
	}
}
