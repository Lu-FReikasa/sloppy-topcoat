package voice

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"topcoat/node/internal/diskfree"
	"topcoat/node/internal/hf"
)

// Kinds of voice model a download can target (models.download target_kind).
const (
	KindSTT = "stt" // whisper.cpp ggml-*.bin, stored in voice-models/stt
	KindTTS = "tts" // a Piper voice (.onnx + .onnx.json), stored in voice-models/tts
)

// PiperVoicesRepo is the Hugging Face repo of the Piper voices.
const PiperVoicesRepo = "rhasspy/piper-voices"

// PiperVoicesRevision is the revision of PiperVoicesRepo a voice is fetched at when the request
// names none: the voice configs written later (by Piper 1.5, from 2026-11-17) use phonemes of
// several code points, which the pinned standalone piper binary aborts on ("Phonemes must be one
// codepoint", verified 2026-11-25 with de_DE-thorsten-medium).
// UNVERIFIED against the real huggingface.co: that it resolves this abbreviated commit id.
const PiperVoicesRevision = "a60bfb135881"

// ErrModelExists is returned when a voice model of that name is already on the node.
var ErrModelExists = errors.New("already exists on this node")

// ErrInsufficientDisk is returned (wrapped) when a download would not fit onto the drive.
var ErrInsufficientDisk = errors.New("insufficient disk space")

// ErrBadVoiceConfig is returned when a Piper voice config cannot be used by the pinned binary.
var ErrBadVoiceConfig = errors.New("voice config not usable by the pinned piper")

// DownloadRequest is one voice model download (models.download with target_kind stt or tts).
type DownloadRequest struct {
	Kind       string
	Repo       string
	File       string // stt: a .bin file; tts: the voice's .onnx (its .onnx.json comes along)
	Revision   string
	TargetName string
	// Progress is called with downloaded / total bytes of the model file (may be nil).
	Progress hf.Progress
}

// Downloader fetches voice models from Hugging Face into <data dir>/voice-models.
type Downloader struct {
	DataDir string
	HF      *hf.Client
	// DiskFree measures the drive (default diskfree.Of); ReserveMB stays free (default 1024).
	DiskFree  diskfree.Func
	ReserveMB int64
}

func (d *Downloader) dir(kind string) string { return filepath.Join(d.DataDir, "voice-models", kind) }

func (d *Downloader) reserve() int64 {
	if d.ReserveMB <= 0 {
		return 1024
	}
	return d.ReserveMB
}

// Name validates req (and fills in the default revision) and returns the name the model will
// have: target_name, or the file name without directory and extension.
func (d *Downloader) Name(req *DownloadRequest) (string, error) {
	var ext string
	switch req.Kind {
	case KindSTT:
		ext = ".bin"
	case KindTTS:
		ext = ".onnx"
		if req.Revision == "" && req.Repo == PiperVoicesRepo {
			req.Revision = PiperVoicesRevision
		}
	default:
		return "", fmt.Errorf("target_kind %q is not a voice kind (stt or tts)", req.Kind)
	}
	src := hf.Source{Repo: req.Repo, File: req.File, Revision: req.Revision}
	if err := src.ValidateFile(ext); err != nil {
		return "", err
	}
	req.Revision = src.Revision
	name := strings.TrimSpace(req.TargetName)
	if name == "" {
		name = strings.TrimSuffix(path.Base(req.File), ext)
	} else {
		name = strings.TrimSuffix(name, ext)
	}
	if !nameRe.MatchString(name) {
		return "", fmt.Errorf("model name %q: use letters, digits, '.', '_' or '-'", name)
	}
	return name, nil
}

func (d *Downloader) dest(req DownloadRequest, name string) string {
	ext := ".bin"
	if req.Kind == KindTTS {
		ext = ".onnx"
	}
	return filepath.Join(d.dir(req.Kind), name+ext)
}

func (d *Downloader) sources(req DownloadRequest) (model, config hf.Source) {
	model = hf.Source{Repo: req.Repo, File: req.File, Revision: req.Revision}
	if req.Kind == KindTTS {
		config = hf.Source{Repo: req.Repo, File: req.File + ".json", Revision: req.Revision, AllowGitBlob: true}
	}
	return model, config
}

// Check refuses a download whose model exists (ErrModelExists) or that does not fit onto the
// drive (ErrInsufficientDisk, size from the Hugging Face metadata minus a resumable .part plus
// the reserve). Other errors mean "could not check". req must have passed Name.
func (d *Downloader) Check(ctx context.Context, req DownloadRequest, name string) error {
	dest := d.dest(req, name)
	if _, err := os.Stat(dest); err == nil {
		return fmt.Errorf("%s model %s %w", req.Kind, name, ErrModelExists)
	}
	model, config := d.sources(req)
	info, err := d.HF.Info(ctx, model)
	if err != nil {
		return err
	}
	need := info.Size
	if config.File != "" {
		ci, err := d.HF.Info(ctx, config)
		if err != nil {
			return err
		}
		need += ci.Size
	}
	if st, err := os.Stat(dest + ".part"); err == nil {
		need -= st.Size()
	}
	need = max(need, 0)
	df := d.DiskFree
	if df == nil {
		df = diskfree.Of
	}
	dir := d.dir(req.Kind)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return err
	}
	free, err := df(dir)
	if err != nil {
		return err
	}
	if free.FreeBytes < need+d.reserve()<<20 {
		return fmt.Errorf("%w: %s needs %d MB, %d MB must stay free, but only %d MB are free on the drive of %s",
			ErrInsufficientDisk, req.File, need>>20, d.reserve(), free.FreeBytes>>20, dir)
	}
	return nil
}

// Download fetches the model (resumable, verified) and returns its size in bytes. A Piper
// voice's config comes first and is checked before the large .onnx is fetched; the voice only
// shows up (Speaker.Voices) once both files are in place. req must have passed Name.
func (d *Downloader) Download(ctx context.Context, req DownloadRequest, name string) (int64, error) {
	if err := d.Check(ctx, req, name); errors.Is(err, ErrModelExists) || errors.Is(err, ErrInsufficientDisk) {
		return 0, err
	}
	dest := d.dest(req, name)
	if err := os.MkdirAll(filepath.Dir(dest), 0o750); err != nil {
		return 0, err
	}
	model, config := d.sources(req)
	var size int64
	if config.File != "" {
		cdest := dest + ".json"
		ci, err := d.HF.Download(ctx, config, cdest, nil)
		if err != nil {
			return 0, fmt.Errorf("voice config: %w", err)
		}
		if err := checkPiperConfig(cdest); err != nil {
			_ = os.Remove(cdest)
			return 0, fmt.Errorf("%s@%s: %w", config.File, config.Revision, err)
		}
		size += ci.Size
	}
	info, err := d.HF.Download(ctx, model, dest, req.Progress)
	if err != nil {
		return 0, err
	}
	return size + info.Size, nil
}

// checkPiperConfig refuses a voice config the pinned piper binary would abort on: every key of
// phoneme_id_map must be a single code point.
func checkPiperConfig(file string) error {
	b, err := os.ReadFile(file)
	if err != nil {
		return err
	}
	var cfg struct {
		PhonemeIDMap map[string]json.RawMessage `json:"phoneme_id_map"`
	}
	if err := json.Unmarshal(b, &cfg); err != nil {
		return fmt.Errorf("%w: %w", ErrBadVoiceConfig, err)
	}
	if len(cfg.PhonemeIDMap) == 0 {
		return fmt.Errorf("%w: no phoneme_id_map", ErrBadVoiceConfig)
	}
	for k := range cfg.PhonemeIDMap {
		if utf8.RuneCountInString(k) != 1 {
			return fmt.Errorf("%w: phoneme %q is more than one code point (fetch the voice at revision %s of %s)",
				ErrBadVoiceConfig, k, PiperVoicesRevision, PiperVoicesRepo)
		}
	}
	return nil
}
