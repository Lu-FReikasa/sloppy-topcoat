package voice

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"topcoat/node/internal/llamacpp"
	"topcoat/node/internal/proc"
)

// Exec runs bin with args in dir, with extra environment and stdin; it returns stderr for error
// messages. Injectable for tests.
type Exec func(ctx context.Context, bin string, args []string, dir string, env []string, stdin []byte) (stderr []byte, err error)

// RealExec runs the program at low priority (proc.CommandContext), as llama-server runs.
func RealExec(ctx context.Context, bin string, args []string, dir string, env []string, stdin []byte) ([]byte, error) {
	cmd := proc.CommandContext(ctx, bin, args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), env...)
	cmd.Stdin = bytes.NewReader(stdin)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	err := cmd.Run()
	return stderr.Bytes(), err
}

// Options configure the real engines.
type Options struct {
	// DataDir is the node data dir: voice-models/stt (whisper ggml-*.bin), voice-models/tts
	// (Piper *.onnx with *.onnx.json) and runtime-voice/ live below it.
	DataDir string
	// WhisperBackend is cpu or cuda (Windows only); Threads the -t of whisper-cli (0 = 4).
	WhisperBackend string
	Threads        int
	Whisper        *llamacpp.Installer
	Piper          *llamacpp.Installer
	Exec           Exec
	Timeout        time.Duration // per request, default 2 min
}

func (o *Options) defaults() {
	if o.Exec == nil {
		o.Exec = RealExec
	}
	if o.Timeout <= 0 {
		o.Timeout = 2 * time.Minute
	}
	if o.Threads <= 0 {
		o.Threads = 4
	}
	if o.WhisperBackend == "" {
		o.WhisperBackend = llamacpp.BackendCPU
	}
}

// NewInstallers returns the installers of the pinned builds below <data dir>/runtime-voice.
func NewInstallers(dataDir string) (whisper, piper *llamacpp.Installer) {
	root := filepath.Join(dataDir, "runtime-voice")
	whisper = llamacpp.NewInstaller(filepath.Join(root, "whisper"))
	whisper.Manifest, whisper.Executables = WhisperPins(), []string{"whisper-cli"}
	piper = llamacpp.NewInstaller(filepath.Join(root, "piper"))
	piper.Manifest, piper.Executables = PiperPins(), []string{"piper"}
	return whisper, piper
}

// WhisperBackend picks the whisper.cpp build: CUDA only on Windows with an NVIDIA GPU (there is
// neither a Linux GPU build nor a Vulkan one), otherwise the CPU.
func WhisperBackend(goos, configured string, vendors []string) string {
	if configured == llamacpp.BackendCPU {
		return configured
	}
	if goos == "windows" {
		for _, v := range vendors {
			if v == "nvidia" {
				return llamacpp.BackendCUDA
			}
		}
	}
	return llamacpp.BackendCPU
}

// libEnv lets the program find the libraries next to it (Linux; Windows looks there anyway).
func libEnv(dir string) []string {
	if runtime.GOOS == "windows" {
		return nil
	}
	return []string{"LD_LIBRARY_PATH=" + dir}
}

// --- whisper.cpp -------------------------------------------------------------------------

// Whisper is the speech-to-text engine: whisper-cli per request.
type Whisper struct{ o Options }

// NewWhisper returns the engine; the build is installed on its first use.
func NewWhisper(o Options) *Whisper { o.defaults(); return &Whisper{o: o} }

func (w *Whisper) dir() string { return filepath.Join(w.o.DataDir, "voice-models", "stt") }

// Models implements Transcriber: ggml-*.bin files, named without the extension.
func (w *Whisper) Models() []string { return listFiles(w.dir(), ".bin", "") }

// Transcribe implements Transcriber.
// UNVERIFIED on real hardware: through a node (verified by hand with the pinned Linux build).
func (w *Whisper) Transcribe(ctx context.Context, wav []byte, model, language string) (Transcript, error) {
	model, err := pick(w.Models(), model, "speech-to-text model")
	if err != nil {
		return Transcript{}, err
	}
	inst, err := w.o.Whisper.Ensure(ctx, w.o.Whisper.Manifest.Default, w.o.WhisperBackend, nil)
	if err != nil {
		return Transcript{}, fmt.Errorf("install whisper.cpp: %w", err)
	}
	tmp, err := os.MkdirTemp("", "topcoat-stt-")
	if err != nil {
		return Transcript{}, err
	}
	defer os.RemoveAll(tmp) //nolint:errcheck // best effort: audio is not kept (D127)
	in := filepath.Join(tmp, "in.wav")
	if err := os.WriteFile(in, wav, 0o600); err != nil {
		return Transcript{}, err
	}
	if language == "" {
		language = "auto"
	}
	args := []string{"-m", filepath.Join(w.dir(), model+".bin"), "-f", in, "-l", language,
		"-oj", "-of", filepath.Join(tmp, "out"), "-nt", "-np", "-t", fmt.Sprint(w.o.Threads)}
	ctx, cancel := context.WithTimeout(ctx, w.o.Timeout)
	defer cancel()
	if stderr, err := w.o.Exec(ctx, inst.Server, args, inst.Dir, libEnv(inst.Dir), nil); err != nil {
		return Transcript{}, fmt.Errorf("whisper-cli: %w: %s", err, lastLine(stderr))
	}
	raw, err := os.ReadFile(filepath.Join(tmp, "out.json"))
	if err != nil {
		return Transcript{}, fmt.Errorf("whisper-cli wrote no result: %w", err)
	}
	return parseWhisperJSON(raw)
}

// parseWhisperJSON reads whisper-cli's -oj output.
func parseWhisperJSON(raw []byte) (Transcript, error) {
	var out struct {
		Result struct {
			Language string `json:"language"`
		} `json:"result"`
		Transcription []struct {
			Text string `json:"text"`
		} `json:"transcription"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return Transcript{}, fmt.Errorf("whisper-cli result: %w", err)
	}
	parts := make([]string, 0, len(out.Transcription))
	for _, s := range out.Transcription {
		if t := strings.TrimSpace(s.Text); t != "" {
			parts = append(parts, t)
		}
	}
	return Transcript{Text: strings.Join(parts, " "), Language: out.Result.Language}, nil
}

// --- Piper -------------------------------------------------------------------------------

// Piper is the text-to-speech engine: piper per request, the text on stdin.
type Piper struct{ o Options }

// NewPiper returns the engine; the build is installed on its first use.
func NewPiper(o Options) *Piper { o.defaults(); return &Piper{o: o} }

func (p *Piper) dir() string { return filepath.Join(p.o.DataDir, "voice-models", "tts") }

// Voices implements Speaker: *.onnx files that have their *.onnx.json next to them.
func (p *Piper) Voices() []string { return listFiles(p.dir(), ".onnx", ".json") }

// Speak implements Speaker and returns a WAV file.
// UNVERIFIED on real hardware: through a node (verified by hand with the pinned Linux build).
func (p *Piper) Speak(ctx context.Context, text, voice string) ([]byte, error) {
	voice, err := pick(p.Voices(), voice, "voice")
	if err != nil {
		return nil, err
	}
	inst, err := p.o.Piper.Ensure(ctx, p.o.Piper.Manifest.Default, llamacpp.BackendCPU, nil)
	if err != nil {
		return nil, fmt.Errorf("install piper: %w", err)
	}
	tmp, err := os.MkdirTemp("", "topcoat-tts-")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(tmp) //nolint:errcheck // best effort
	out := filepath.Join(tmp, "out.wav")
	args := []string{"--model", filepath.Join(p.dir(), voice+".onnx"),
		"--espeak_data", filepath.Join(inst.Dir, "espeak-ng-data"), "--output_file", out}
	ctx, cancel := context.WithTimeout(ctx, p.o.Timeout)
	defer cancel()
	// one line per sentence would be read as separate utterances; the text is one
	line := strings.Join(strings.Fields(text), " ") + "\n"
	if stderr, err := p.o.Exec(ctx, inst.Server, args, inst.Dir, libEnv(inst.Dir), []byte(line)); err != nil {
		return nil, fmt.Errorf("piper: %w: %s", err, lastLine(stderr))
	}
	b, err := os.ReadFile(out)
	if err != nil {
		return nil, fmt.Errorf("piper wrote no audio: %w", err)
	}
	return b, nil
}

func lastLine(b []byte) string {
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	return strings.TrimSpace(lines[len(lines)-1])
}
