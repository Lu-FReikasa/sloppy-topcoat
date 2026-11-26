package voice

import (
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"strings"
	"unicode/utf8"

	"topcoat/node/internal/providers/capability"
)

// Register adds voice.transcribe (when t is set) and voice.speak (when s is set) to reg.
func Register(reg *capability.Registry, t Transcriber, s Speaker) error {
	if t != nil {
		if err := reg.Register(transcribeCap{t}); err != nil {
			return err
		}
	}
	if s != nil {
		if err := reg.Register(speakCap{s}); err != nil {
			return err
		}
	}
	return nil
}

// --- voice.transcribe --------------------------------------------------------------------

type transcribeCap struct{ t Transcriber }

func (transcribeCap) Name() string { return "voice.transcribe" }

func (c transcribeCap) Info() capability.Info {
	return capability.Info{
		Description: "Speech to text (whisper.cpp): a WAV recording in, its text and language out. Models: " +
			strings.Join(c.t.Models(), ", "),
		ReadOnly: true, // reads the audio it is given; nothing on the machine changes, nothing is kept
	}
}

func (transcribeCap) InputSchema() json.RawMessage {
	return json.RawMessage(`{"type":"object","additionalProperties":false,"required":["audio_base64"],
"properties":{"audio_base64":{"type":"string","minLength":1,"maxLength":3400000,"description":"a WAV file, base64"},
"model":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$","description":"default: the first model of the node"},
"language":{"type":"string","pattern":"^(auto|[a-z]{2,3})$","description":"default auto"}}}`)
}

func (c transcribeCap) Invoke(ctx context.Context, input json.RawMessage) (json.RawMessage, error) {
	var in struct {
		Audio    string `json:"audio_base64"`
		Model    string `json:"model"`
		Language string `json:"language"`
	}
	if err := strictDecode(input, &in); err != nil {
		return nil, err
	}
	if in.Model != "" && !nameRe.MatchString(in.Model) {
		return nil, fmt.Errorf("%w: %q", ErrUnknownModel, in.Model)
	}
	wav, err := base64.StdEncoding.DecodeString(in.Audio)
	if err != nil {
		return nil, fmt.Errorf("audio_base64: %w", err)
	}
	if len(wav) > MaxAudioBytes {
		return nil, fmt.Errorf("audio %w: %d bytes, at most %d", ErrTooLong, len(wav), MaxAudioBytes)
	}
	if !isWAV(wav) {
		return nil, ErrBadAudio
	}
	tr, err := c.t.Transcribe(ctx, wav, in.Model, in.Language)
	if err != nil {
		return nil, err
	}
	return json.Marshal(tr)
}

// --- voice.speak -------------------------------------------------------------------------

type speakCap struct{ s Speaker }

func (speakCap) Name() string { return "voice.speak" }

func (c speakCap) Info() capability.Info {
	return capability.Info{
		Description: "Text to speech (Piper): a text in, a WAV recording out. Voices: " + strings.Join(c.s.Voices(), ", "),
		ReadOnly:    true,
	}
}

func (speakCap) InputSchema() json.RawMessage {
	return json.RawMessage(`{"type":"object","additionalProperties":false,"required":["text"],
"properties":{"text":{"type":"string","minLength":1,"maxLength":600},
"voice":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$","description":"default: the first voice of the node"}}}`)
}

func (c speakCap) Invoke(ctx context.Context, input json.RawMessage) (json.RawMessage, error) {
	var in struct {
		Text  string `json:"text"`
		Voice string `json:"voice"`
	}
	if err := strictDecode(input, &in); err != nil {
		return nil, err
	}
	if strings.TrimSpace(in.Text) == "" {
		return nil, fmt.Errorf("text is empty")
	}
	if n := utf8.RuneCountInString(in.Text); n > MaxTextRunes {
		return nil, fmt.Errorf("text %w: %d characters, at most %d", ErrTooLong, n, MaxTextRunes)
	}
	if in.Voice != "" && !nameRe.MatchString(in.Voice) {
		return nil, fmt.Errorf("%w: %q", ErrUnknownModel, in.Voice)
	}
	wav, err := c.s.Speak(ctx, in.Text, in.Voice)
	if err != nil {
		return nil, err
	}
	if len(wav) > MaxSpeechBytes {
		return nil, fmt.Errorf("speech %w for one message (%d bytes): send shorter texts", ErrTooLong, len(wav))
	}
	return json.Marshal(map[string]any{"audio_base64": base64.StdEncoding.EncodeToString(wav), "format": "wav"})
}

func strictDecode(input json.RawMessage, v any) error {
	dec := json.NewDecoder(strings.NewReader(string(input)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return fmt.Errorf("input: %w", err)
	}
	return nil
}

// --- fakes -------------------------------------------------------------------------------

// FakeTranscriber answers with a fixed text (fake nodes and tests).
type FakeTranscriber struct{ Text, Language string }

// Models implements Transcriber.
func (FakeTranscriber) Models() []string { return []string{"fake-whisper"} }

// Transcribe implements Transcriber.
func (f FakeTranscriber) Transcribe(_ context.Context, wav []byte, model, _ string) (Transcript, error) {
	if model != "" && model != "fake-whisper" {
		return Transcript{}, fmt.Errorf("%w: %q", ErrUnknownModel, model)
	}
	return Transcript(f), nil
}

// FakeSpeaker returns a short silent WAV (fake nodes and tests).
type FakeSpeaker struct{}

// Voices implements Speaker.
func (FakeSpeaker) Voices() []string { return []string{"fake-voice"} }

// Speak implements Speaker.
func (FakeSpeaker) Speak(_ context.Context, text, voice string) ([]byte, error) {
	if voice != "" && voice != "fake-voice" {
		return nil, fmt.Errorf("%w: %q", ErrUnknownModel, voice)
	}
	return SilentWAV(len(text) * 100), nil
}

// SilentWAV is a 16 kHz mono 16-bit WAV of n silent samples (n is clamped to 0..maxSilent).
func SilentWAV(n int) []byte {
	n = min(max(n, 0), maxSilent)
	data := n * 2
	b := make([]byte, 44+data)
	copy(b[0:], "RIFF")
	le32(b[4:], uint32(36+data)) //nolint:gosec // data is at most 2*maxSilent
	copy(b[8:], "WAVEfmt ")
	le32(b[16:], 16)
	b[20], b[22] = 1, 1 // PCM, mono
	le32(b[24:], 16000)
	le32(b[28:], 32000)
	b[32], b[34] = 2, 16
	copy(b[36:], "data")
	le32(b[40:], uint32(data)) //nolint:gosec // data is at most 2*maxSilent
	return b
}

// maxSilent bounds SilentWAV: ten minutes at 16 kHz.
const maxSilent = 16000 * 600

func le32(b []byte, v uint32) { binary.LittleEndian.PutUint32(b, v) }
