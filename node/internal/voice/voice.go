// Package voice is the node's speech service (D127): speech to text with whisper.cpp and text to
// speech with Piper. Both are pinned official builds (whisper.json, piper.json; verified by
// SHA-256 and unpacked by the llama.cpp installer) that run as a short process per request: no
// server, no port at all. They are offered as the capabilities voice.transcribe and voice.speak,
// which the hub assigns per node like every capability (no generic shell).
package voice

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"topcoat/node/internal/llamacpp"
)

//go:embed whisper.json
var whisperJSON []byte

//go:embed piper.json
var piperJSON []byte

var (
	whisperTag = regexp.MustCompile(`^b[0-9]+$`)
	piperTag   = regexp.MustCompile(`^[0-9]{4}\.[0-9]{2}\.[0-9]{2}-[0-9]+$`)
	// a model or voice name: a plain file name without extension, never a path
	nameRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
)

// WhisperPins and PiperPins are the manifests compiled into this binary.
func WhisperPins() llamacpp.Manifest { return mustParse(whisperJSON, whisperTag) }
func PiperPins() llamacpp.Manifest   { return mustParse(piperJSON, piperTag) }

func mustParse(b []byte, tag *regexp.Regexp) llamacpp.Manifest {
	m, err := llamacpp.ParseManifestFor(b, tag)
	if err != nil {
		panic("voice: embedded pins: " + err.Error())
	}
	return m
}

// Limits of one request. Audio and speech travel base64-encoded inside one session message of
// at most 4 MB (agent/session.go), so they stay well below 3 MB.
const (
	MaxAudioBytes  = 2_500_000 // ~78 s of 16 kHz mono 16-bit WAV
	MaxSpeechBytes = 2_800_000 // ~63 s of Piper's 22 kHz mono 16-bit WAV
	MaxTextRunes   = 600
)

// Errors the capabilities return.
var (
	ErrUnknownModel = errors.New("unknown voice model")
	ErrBadAudio     = errors.New("audio must be a WAV file")
	ErrTooLong      = errors.New("too long")
)

// Transcript is what speech to text returns.
type Transcript struct {
	Text     string `json:"text"`
	Language string `json:"language"`
}

// Transcriber turns speech into text (whisper.cpp; fakes in tests).
type Transcriber interface {
	Models() []string
	Transcribe(ctx context.Context, wav []byte, model, language string) (Transcript, error)
}

// Speaker turns text into speech (Piper; fakes in tests).
type Speaker interface {
	Voices() []string
	Speak(ctx context.Context, text, voice string) ([]byte, error)
}

// listFiles returns the names (without ext) of the files in dir ending in ext whose name is
// valid; with need, only those that also have the file name+need (Piper's .onnx.json).
func listFiles(dir, ext, need string) []string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range entries {
		n := e.Name()
		if e.IsDir() || !strings.HasSuffix(strings.ToLower(n), ext) {
			continue
		}
		base := n[:len(n)-len(ext)]
		if !nameRe.MatchString(base) {
			continue
		}
		if need != "" {
			if _, err := os.Stat(filepath.Join(dir, n+need)); err != nil {
				continue
			}
		}
		out = append(out, base)
	}
	sort.Strings(out)
	return out
}

// pick resolves name in list; "" picks the first one.
func pick(list []string, name, what string) (string, error) {
	if len(list) == 0 {
		return "", fmt.Errorf("%w: this node has no %s", ErrUnknownModel, what)
	}
	if name == "" {
		return list[0], nil
	}
	for _, n := range list {
		if n == name {
			return n, nil
		}
	}
	return "", fmt.Errorf("%w: %s %q (have: %s)", ErrUnknownModel, what, name, strings.Join(list, ", "))
}

// isWAV reports whether b looks like a RIFF/WAVE file.
func isWAV(b []byte) bool {
	return len(b) > 44 && string(b[0:4]) == "RIFF" && string(b[8:12]) == "WAVE"
}
