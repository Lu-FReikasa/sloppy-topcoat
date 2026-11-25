package voice

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"topcoat/node/internal/llamacpp"
	"topcoat/node/internal/providers/capability"
)

func TestThePinsHaveTheBuildsTheNodesUse(t *testing.T) {
	w, p := WhisperPins(), PiperPins()
	for _, b := range [][2]string{{"linux", "cpu"}, {"windows", "cpu"}, {"windows", "cuda"}} {
		if _, err := w.Build(w.Default, b[0], b[1]); err != nil {
			t.Errorf("whisper %v: %v", b, err)
		}
	}
	for _, b := range [][2]string{{"linux", "cpu"}, {"windows", "cpu"}} {
		if _, err := p.Build(p.Default, b[0], b[1]); err != nil {
			t.Errorf("piper %v: %v", b, err)
		}
	}
	if got := WhisperBackend("linux", "auto", []string{"nvidia"}); got != "cpu" {
		t.Errorf("there is no Linux GPU build of whisper.cpp: %s", got)
	}
	if got := WhisperBackend("windows", "auto", []string{"nvidia"}); got != "cuda" {
		t.Errorf("windows nvidia: %s", got)
	}
	if got := WhisperBackend("windows", "auto", []string{"amd"}); got != "cpu" {
		t.Errorf("windows amd (no Vulkan build): %s", got)
	}
}

// installedFake marks a build of in as installed, with its server program at exe.
func installedFake(t *testing.T, in *llamacpp.Installer, backend, exe string) {
	t.Helper()
	dir := filepath.Join(in.Root, in.Manifest.Default+"-"+backend)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	server := filepath.Join(dir, exe)
	_ = os.WriteFile(server, []byte("#!"), 0o755)
	marker := fmt.Sprintf(`{"release":%q,"backend":%q,"dir":%q,"server":%q}`, in.Manifest.Default, backend, dir, server)
	if err := os.WriteFile(filepath.Join(dir, "topcoat-install.json"), []byte(marker), 0o644); err != nil {
		t.Fatal(err)
	}
}

func setup(t *testing.T, exec Exec) (*Whisper, *Piper, string) {
	t.Helper()
	data := t.TempDir()
	w, p := NewInstallers(data)
	installedFake(t, w, "cpu", "whisper-cli")
	installedFake(t, p, "cpu", "piper")
	o := Options{DataDir: data, Whisper: w, Piper: p, Exec: exec}
	return NewWhisper(o), NewPiper(o), data
}

func write(t *testing.T, path string, b []byte) {
	t.Helper()
	_ = os.MkdirAll(filepath.Dir(path), 0o755)
	if err := os.WriteFile(path, b, 0o644); err != nil {
		t.Fatal(err)
	}
}

func argAfter(args []string, flag string) string {
	if i := slices.Index(args, flag); i >= 0 && i+1 < len(args) {
		return args[i+1]
	}
	return ""
}

func TestWhisperRunsOncePerRequestAndReadsItsJSON(t *testing.T) {
	var seen []string
	var tmpDir string
	w, _, data := setup(t, func(_ context.Context, bin string, args []string, _ string, _ []string, _ []byte) ([]byte, error) {
		seen = append([]string{bin}, args...)
		out := argAfter(args, "-of")
		tmpDir = filepath.Dir(out)
		in, _ := os.ReadFile(argAfter(args, "-f"))
		if !isWAV(in) {
			return []byte("not a wav"), errors.New("exit status 1")
		}
		return nil, os.WriteFile(out+".json", []byte(`{"result":{"language":"de"},"transcription":[{"text":" Guten Abend."},{"text":" Wie geht es?"}]}`), 0o600)
	})
	write(t, filepath.Join(data, "voice-models", "stt", "ggml-base.bin"), []byte("x"))
	write(t, filepath.Join(data, "voice-models", "stt", "notes.txt"), []byte("x"))
	if got := w.Models(); !slices.Equal(got, []string{"ggml-base"}) {
		t.Fatalf("models %v", got)
	}
	tr, err := w.Transcribe(context.Background(), SilentWAV(100), "", "")
	if err != nil {
		t.Fatal(err)
	}
	if tr.Text != "Guten Abend. Wie geht es?" || tr.Language != "de" {
		t.Fatalf("%+v", tr)
	}
	if !strings.HasSuffix(seen[0], "whisper-cli") || argAfter(seen, "-l") != "auto" || !slices.Contains(seen, "-oj") ||
		argAfter(seen, "-m") != filepath.Join(data, "voice-models", "stt", "ggml-base.bin") {
		t.Fatalf("args %v", seen)
	}
	if _, err := os.Stat(tmpDir); !os.IsNotExist(err) {
		t.Fatal("the audio must not stay on disk (D127)")
	}
	if _, err := w.Transcribe(context.Background(), SilentWAV(100), "ggml-large", ""); !errors.Is(err, ErrUnknownModel) {
		t.Fatalf("%v", err)
	}
}

func TestPiperGetsTheTextAsOneLineAndNeedsTheVoiceConfig(t *testing.T) {
	var stdinSeen string
	_, p, data := setup(t, func(_ context.Context, _ string, args []string, _ string, env []string, stdin []byte) ([]byte, error) {
		stdinSeen = string(stdin)
		if !strings.HasSuffix(argAfter(args, "--espeak_data"), "espeak-ng-data") {
			return []byte("no espeak data"), errors.New("exit status 1")
		}
		return nil, os.WriteFile(argAfter(args, "--output_file"), SilentWAV(10), 0o600)
	})
	dir := filepath.Join(data, "voice-models", "tts")
	write(t, filepath.Join(dir, "de_DE-thorsten-medium.onnx"), []byte("x"))
	write(t, filepath.Join(dir, "de_DE-thorsten-medium.onnx.json"), []byte("{}"))
	write(t, filepath.Join(dir, "no-config.onnx"), []byte("x")) // unusable without its .onnx.json
	if got := p.Voices(); !slices.Equal(got, []string{"de_DE-thorsten-medium"}) {
		t.Fatalf("voices %v", got)
	}
	wav, err := p.Speak(context.Background(), "Guten Abend.\nWie geht es?", "")
	if err != nil {
		t.Fatal(err)
	}
	if !isWAV(wav) || stdinSeen != "Guten Abend. Wie geht es?\n" {
		t.Fatalf("stdin %q", stdinSeen)
	}
}

func invoke(t *testing.T, reg *capability.Registry, name string, in any) (map[string]any, error) {
	t.Helper()
	c, ok := reg.Get(name)
	if !ok {
		t.Fatalf("%s not registered", name)
	}
	raw, _ := json.Marshal(in)
	out, err := c.Invoke(context.Background(), raw)
	if err != nil {
		return nil, err
	}
	var m map[string]any
	_ = json.Unmarshal(out, &m)
	return m, nil
}

func TestTheCapabilitiesCheckTheirInput(t *testing.T) {
	reg := capability.NewRegistry()
	if err := Register(reg, FakeTranscriber{Text: "hallo", Language: "de"}, FakeSpeaker{}); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"voice.transcribe", "voice.speak"} {
		c, _ := reg.Get(name)
		if !capability.InfoOf(c).ReadOnly || !json.Valid(c.InputSchema()) {
			t.Errorf("%s: read-only with a valid schema", name)
		}
	}
	wav := base64.StdEncoding.EncodeToString(SilentWAV(1000))
	got, err := invoke(t, reg, "voice.transcribe", map[string]any{"audio_base64": wav, "language": "de"})
	if err != nil || got["text"] != "hallo" || got["language"] != "de" {
		t.Fatalf("%v %v", got, err)
	}
	if _, err := invoke(t, reg, "voice.transcribe", map[string]any{"audio_base64": base64.StdEncoding.EncodeToString([]byte("ID3 an mp3"))}); !errors.Is(err, ErrBadAudio) {
		t.Fatalf("only WAV: %v", err)
	}
	big := base64.StdEncoding.EncodeToString(SilentWAV(MaxAudioBytes))
	if _, err := invoke(t, reg, "voice.transcribe", map[string]any{"audio_base64": big}); !errors.Is(err, ErrTooLong) {
		t.Fatalf("too long: %v", err)
	}
	if _, err := invoke(t, reg, "voice.transcribe", map[string]any{"audio_base64": wav, "model": "../etc/passwd"}); !errors.Is(err, ErrUnknownModel) {
		t.Fatalf("a path is no model name: %v", err)
	}
	if _, err := invoke(t, reg, "voice.transcribe", map[string]any{"audio_base64": wav, "shell": "x"}); err == nil {
		t.Fatal("unknown input field accepted")
	}

	got, err = invoke(t, reg, "voice.speak", map[string]any{"text": "Guten Abend"})
	if err != nil || got["format"] != "wav" {
		t.Fatalf("%v %v", got, err)
	}
	audio, _ := base64.StdEncoding.DecodeString(got["audio_base64"].(string))
	if !isWAV(audio) {
		t.Fatal("speak returns a WAV")
	}
	if _, err := invoke(t, reg, "voice.speak", map[string]any{"text": strings.Repeat("ä", MaxTextRunes+1)}); !errors.Is(err, ErrTooLong) {
		t.Fatalf("too long: %v", err)
	}
	if _, err := invoke(t, reg, "voice.speak", map[string]any{"text": "x", "voice": "other"}); !errors.Is(err, ErrUnknownModel) {
		t.Fatalf("unknown voice: %v", err)
	}
}
