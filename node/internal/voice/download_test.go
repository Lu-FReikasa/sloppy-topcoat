package voice

import (
	"context"
	"crypto/sha1" //nolint:gosec // git blob ids, as Hugging Face reports them
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"topcoat/node/internal/diskfree"
	"topcoat/node/internal/hf"
)

// fakeHF serves files of one repo: big ones through LFS (SHA-256), *.json in git (blob id).
type fakeHF struct {
	mu        sync.Mutex
	files     map[string][]byte // path -> content
	revisions []string          // revision of every request
	gets      []string
}

func (f *fakeHF) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/models/{owner}/{name}/paths-info/{rev}", func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		p := r.PostForm.Get("paths")
		f.mu.Lock()
		f.revisions = append(f.revisions, r.PathValue("rev"))
		c, ok := f.files[p]
		f.mu.Unlock()
		if !ok {
			fmt.Fprint(w, `[]`)
			return
		}
		if strings.HasSuffix(p, ".json") {
			sum := sha1.Sum([]byte(fmt.Sprintf("blob %d\x00%s", len(c), c))) //nolint:gosec // git blob id
			fmt.Fprintf(w, `[{"type":"file","oid":%q,"size":%d,"path":%q}]`, hex.EncodeToString(sum[:]), len(c), p)
			return
		}
		sum := sha256.Sum256(c)
		fmt.Fprintf(w, `[{"type":"file","oid":"%s","size":%d,"path":%q,"lfs":{"oid":%q,"size":%d}}]`,
			strings.Repeat("a", 40), len(c), p, hex.EncodeToString(sum[:]), len(c))
	})
	mux.HandleFunc("GET /{owner}/{name}/resolve/{rev}/{file...}", func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.revisions = append(f.revisions, r.PathValue("rev"))
		f.gets = append(f.gets, r.PathValue("file"))
		c, ok := f.files[r.PathValue("file")]
		f.mu.Unlock()
		if !ok {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write(c)
	})
	return mux
}

const goodConfig = `{"audio":{"sample_rate":22050},"phoneme_id_map":{"_":[0],"^":[1],"ʃ":[96]}}`

func newDownloader(t *testing.T, files map[string][]byte, free int64) (*Downloader, *fakeHF) {
	t.Helper()
	f := &fakeHF{files: files}
	srv := httptest.NewServer(f.handler())
	t.Cleanup(srv.Close)
	d := &Downloader{
		DataDir:  t.TempDir(),
		HF:       &hf.Client{BaseURL: srv.URL, HTTP: srv.Client()},
		DiskFree: func(string) (diskfree.Usage, error) { return diskfree.Usage{FreeBytes: free, TotalBytes: free}, nil },
	}
	return d, f
}

func TestDownloadWhisperModel(t *testing.T) {
	d, _ := newDownloader(t, map[string][]byte{"ggml-base.bin": []byte("whisper weights")}, 10<<30)
	req := DownloadRequest{Kind: KindSTT, Repo: "ggerganov/whisper.cpp", File: "ggml-base.bin"}
	name, err := d.Name(&req)
	if err != nil || name != "ggml-base" || req.Revision != "main" {
		t.Fatalf("%q %v %+v", name, err, req)
	}
	if err := d.Check(context.Background(), req, name); err != nil {
		t.Fatal(err)
	}
	size, err := d.Download(context.Background(), req, name)
	if err != nil || size != int64(len("whisper weights")) {
		t.Fatalf("%d %v", size, err)
	}
	w := NewWhisper(Options{DataDir: d.DataDir})
	if got := w.Models(); len(got) != 1 || got[0] != "ggml-base" {
		t.Fatalf("models %v", got)
	}
	if err := d.Check(context.Background(), req, name); !errors.Is(err, ErrModelExists) {
		t.Fatalf("second check: %v", err)
	}
}

func TestDownloadPiperVoiceAtPinnedRevision(t *testing.T) {
	file := "de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx"
	d, f := newDownloader(t, map[string][]byte{file: []byte("onnx weights"), file + ".json": []byte(goodConfig)}, 10<<30)
	req := DownloadRequest{Kind: KindTTS, Repo: PiperVoicesRepo, File: file, TargetName: "thorsten"}
	name, err := d.Name(&req)
	if err != nil || name != "thorsten" || req.Revision != PiperVoicesRevision {
		t.Fatalf("%q %v %+v", name, err, req)
	}
	size, err := d.Download(context.Background(), req, name)
	if err != nil || size != int64(len("onnx weights")+len(goodConfig)) {
		t.Fatalf("%d %v", size, err)
	}
	for _, r := range f.revisions {
		if r != PiperVoicesRevision {
			t.Fatalf("fetched at %q, want the pinned revision", r)
		}
	}
	p := NewPiper(Options{DataDir: d.DataDir})
	if got := p.Voices(); len(got) != 1 || got[0] != "thorsten" {
		t.Fatalf("voices %v", got)
	}
	if b, _ := os.ReadFile(filepath.Join(d.DataDir, "voice-models", "tts", "thorsten.onnx.json")); string(b) != goodConfig {
		t.Fatalf("config %q", b)
	}
}

func TestDownloadPiperVoiceExplicitRevisionWins(t *testing.T) {
	d, _ := newDownloader(t, nil, 10<<30)
	req := DownloadRequest{Kind: KindTTS, Repo: PiperVoicesRepo, File: "v.onnx", Revision: "v1.0.0"}
	if _, err := d.Name(&req); err != nil || req.Revision != "v1.0.0" {
		t.Fatalf("%v %+v", err, req)
	}
	upper := DownloadRequest{Kind: KindTTS, Repo: "Rhasspy/Piper-Voices", File: "v.onnx"}
	if _, err := d.Name(&upper); err != nil || upper.Revision != PiperVoicesRevision {
		t.Fatalf("other case: %v %+v", err, upper)
	}
	other := DownloadRequest{Kind: KindTTS, Repo: "someone/voices", File: "v.onnx"}
	if _, err := d.Name(&other); err != nil || other.Revision != "main" {
		t.Fatalf("%v %+v", err, other)
	}
}

// A voice config written by Piper 1.5 (phonemes of several code points) is refused before the
// large .onnx is fetched, and nothing is left behind.
func TestDownloadPiperVoiceRefusesMultiCodepointPhonemes(t *testing.T) {
	bad := `{"phoneme_id_map":{"_":[0],"t͡s":[5]}}`
	d, f := newDownloader(t, map[string][]byte{"v.onnx": []byte("onnx"), "v.onnx.json": []byte(bad)}, 10<<30)
	req := DownloadRequest{Kind: KindTTS, Repo: "someone/voices", File: "v.onnx"}
	name, _ := d.Name(&req)
	_, err := d.Download(context.Background(), req, name)
	if !errors.Is(err, ErrBadVoiceConfig) || !strings.Contains(err.Error(), PiperVoicesRevision) {
		t.Fatalf("got %v", err)
	}
	for _, g := range f.gets {
		if g == "v.onnx" {
			t.Fatal("the .onnx was fetched although the config is unusable")
		}
	}
	entries, _ := os.ReadDir(filepath.Join(d.DataDir, "voice-models", "tts"))
	if len(entries) != 0 {
		t.Fatalf("left behind: %v", entries)
	}
}

func TestDownloadVoiceChecksDisk(t *testing.T) {
	d, _ := newDownloader(t, map[string][]byte{"v.onnx": make([]byte, 2<<20), "v.onnx.json": []byte(goodConfig)}, 1<<30)
	req := DownloadRequest{Kind: KindTTS, Repo: "someone/voices", File: "v.onnx"}
	name, _ := d.Name(&req)
	// 1 GiB free, 2 MiB needed plus the 1 GiB reserve
	if err := d.Check(context.Background(), req, name); !errors.Is(err, ErrInsufficientDisk) {
		t.Fatalf("got %v", err)
	}
	if _, err := d.Download(context.Background(), req, name); !errors.Is(err, ErrInsufficientDisk) {
		t.Fatalf("got %v", err)
	}
}

func TestDownloadVoiceNameValidation(t *testing.T) {
	d := &Downloader{DataDir: t.TempDir()}
	for _, req := range []DownloadRequest{
		{Kind: "image", Repo: "a/b", File: "x.bin"},
		{Kind: KindSTT, Repo: "a/b", File: "x.gguf"},
		{Kind: KindSTT, Repo: "a/b", File: "../x.bin"},
		{Kind: KindTTS, Repo: "a/b", File: "x.onnx.json"},
		{Kind: KindTTS, Repo: "a/b", File: "x.onnx", TargetName: "../evil"},
		{Kind: KindSTT, Repo: "nope", File: "x.bin"},
	} {
		if _, err := d.Name(&req); err == nil {
			t.Errorf("accepted %+v", req)
		}
	}
}
