package agent

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/fake"
	"topcoat/node/internal/voice"
)

// fakeVoiceModels is a VoiceModelDownloader: the real one's naming, its own refusals.
type fakeVoiceModels struct {
	mu      sync.Mutex
	real    voice.Downloader
	checks  error
	fails   error
	release chan struct{} // Download waits for it when set
	got     []voice.DownloadRequest
}

func (f *fakeVoiceModels) Name(req *voice.DownloadRequest) (string, error) { return f.real.Name(req) }

func (f *fakeVoiceModels) Check(context.Context, voice.DownloadRequest, string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.checks
}

func (f *fakeVoiceModels) set(checks, fails error) {
	f.mu.Lock()
	f.checks, f.fails = checks, fails
	f.mu.Unlock()
}

func (f *fakeVoiceModels) Download(_ context.Context, req voice.DownloadRequest, _ string) (int64, error) {
	if f.release != nil {
		<-f.release
	}
	f.mu.Lock()
	f.got = append(f.got, req)
	fails := f.fails
	f.mu.Unlock()
	if req.Progress != nil {
		req.Progress(5, 10)
	}
	if fails != nil {
		return 0, fails
	}
	return 10, nil
}

func voiceArgs(kind, repo, file string) string {
	return fmt.Sprintf(`{"source":{"type":"huggingface","repo":%q,"file":%q},"target_kind":%q}`, repo, file, kind)
}

func TestVoiceModelDownloadOverSession(t *testing.T) {
	hub := newFakeHub(t)
	vm := &fakeVoiceModels{release: make(chan struct{})}
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), func(o *Options) { o.VoiceModels = vm })
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, bgConfig)

	res := hc.action(t, "models.download", voiceArgs("tts", voice.PiperVoicesRepo, "de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx"))
	if !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	if r := res.Result.(map[string]any); r["model"] != "de_DE-thorsten-medium" || r["kind"] != "tts" {
		t.Fatalf("%v", r)
	}
	// the same voice again while it downloads is busy; a whisper model of that name is not
	if res := hc.action(t, "models.download", voiceArgs("tts", "a/b", "de_DE-thorsten-medium.onnx")); res.OK || res.Error.Code != protocol.ErrBusy {
		t.Fatalf("%+v", res)
	}
	close(vm.release)
	for {
		d := hc.waitEvent(t, protocol.EventDownloadProgress)
		if d["kind"] != "tts" || d["model"] != "de_DE-thorsten-medium" {
			t.Fatalf("%v", d)
		}
		if d["status"] == "done" {
			if d["downloaded_bytes"].(float64) != 10 {
				t.Fatalf("%v", d)
			}
			break
		}
	}
	hc.waitType(t, protocol.TypeInventory)
	vm.mu.Lock()
	if len(vm.got) != 1 || vm.got[0].Revision != voice.PiperVoicesRevision {
		t.Fatalf("%+v", vm.got)
	}
	vm.mu.Unlock()

	// a failed download is reported with its error
	vm.set(nil, errors.New("SHA-256 mismatch"))
	if res := hc.action(t, "models.download", voiceArgs("stt", "ggerganov/whisper.cpp", "ggml-base.bin")); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	for {
		d := hc.waitEvent(t, protocol.EventDownloadProgress)
		if d["status"] == "failed" {
			if d["kind"] != "stt" || d["model"] != "ggml-base" || d["error"] != "SHA-256 mismatch" {
				t.Fatalf("%v", d)
			}
			break
		}
	}
}

func TestVoiceModelDownloadRefusals(t *testing.T) {
	hub := newFakeHub(t)
	vm := &fakeVoiceModels{}
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), func(o *Options) { o.VoiceModels = vm })
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, bgConfig)

	for _, c := range []struct {
		args string
		code string
	}{
		{`{"source":{"type":"huggingface","repo":"a/b","file":"x.bin"},"target_kind":"image"}`, protocol.ErrInvalidArgs},
		{`{"source":{"type":"huggingface","repo":"a/b","file":"x.bin"},"target_kind":7}`, protocol.ErrInvalidArgs},
		{voiceArgs("stt", "a/b", "x.gguf"), protocol.ErrInvalidArgs},
		{voiceArgs("tts", "a/b", "../x.onnx"), protocol.ErrInvalidArgs},
	} {
		if res := hc.action(t, "models.download", c.args); res.OK || res.Error.Code != c.code {
			t.Errorf("%s: %+v", c.args, res)
		}
	}
	vm.set(fmt.Errorf("%w: needs 60 MB", voice.ErrInsufficientDisk), nil)
	if res := hc.action(t, "models.download", voiceArgs("stt", "a/b", "x.bin")); res.OK || res.Error.Code != protocol.ErrInsufficientDisk {
		t.Fatalf("%+v", res)
	}
	vm.set(fmt.Errorf("stt model x %w", voice.ErrModelExists), nil)
	if res := hc.action(t, "models.download", voiceArgs("stt", "a/b", "x.bin")); res.OK || res.Error.Code != protocol.ErrInvalidArgs {
		t.Fatalf("%+v", res)
	}
	// a check that could not be made does not block
	vm.set(errors.New("hugging face unreachable"), nil)
	if res := hc.action(t, "models.download", voiceArgs("stt", "a/b", "x.bin")); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
}

func TestVoiceModelDownloadWithoutDownloader(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, bgConfig)
	if res := hc.action(t, "models.download", voiceArgs("stt", "a/b", "x.bin")); res.OK || res.Error.Code != protocol.ErrNotImplemented {
		t.Fatalf("%+v", res)
	}
}
