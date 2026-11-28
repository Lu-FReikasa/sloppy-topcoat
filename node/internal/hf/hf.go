// Package hf downloads files from Hugging Face (GGUF models, voice models):
// resumable (HTTP Range on a .part file) and verified against the SHA-256 that
// Hugging Face publishes for LFS files, or, for a small file kept in git itself
// (a Piper voice's .onnx.json), against its git blob id when the caller allows it.
//
// UNVERIFIED against the real huggingface.co (the development sandbox cannot
// reach it); tests run against an httptest server that mimics the two
// endpoints used:
//
//	POST {base}/api/models/{repo}/paths-info/{revision}  (form: paths=<file>)
//	     -> [{"type":"file","path":..,"size":..,"lfs":{"oid":"<sha256>","size":..}}]
//	GET  {base}/{repo}/resolve/{revision}/{file}          (302 to the CDN, Range supported)
package hf

import (
	"context"
	"crypto/sha1" //nolint:gosec // git blob ids are SHA-1; only for small files kept in git
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// DefaultBaseURL is the Hugging Face hub.
const DefaultBaseURL = "https://huggingface.co"

var (
	repoRe     = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$`)
	pathRe     = regexp.MustCompile(`^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$`)
	revisionRe = regexp.MustCompile(`^[A-Za-z0-9._-]{1,100}$`)
	splitRe    = regexp.MustCompile(`-\d{5}-of-\d{5}\.gguf$`)
	sha256Re   = regexp.MustCompile(`^[0-9a-f]{64}$`)
	sha1Re     = regexp.MustCompile(`^[0-9a-f]{40}$`)
)

// MaxGitFileBytes is the largest file that may be verified by its git blob id (AllowGitBlob).
const MaxGitFileBytes = 1 << 20

// Source identifies one file on Hugging Face.
type Source struct {
	Repo     string
	File     string
	Revision string // default "main"
	// AllowGitBlob lets a file that is not in LFS be verified by its git blob id (SHA-1 of
	// "blob <size>\x00" + content) instead of a SHA-256, for files up to MaxGitFileBytes.
	// Never set from the wire: a caller sets it for the small config files it knows.
	AllowGitBlob bool
}

// Validate checks the source for safe, supported values of a GGUF model.
func (s *Source) Validate() error {
	if err := s.ValidateFile(".gguf"); err != nil {
		return err
	}
	if splitRe.MatchString(s.File) {
		return fmt.Errorf("file %q is part of a split GGUF; split models are not supported yet", s.File)
	}
	return nil
}

// ValidateFile checks repo, revision and that the file is a path inside the repo ending in
// one of exts (lower case, e.g. ".bin").
func (s *Source) ValidateFile(exts ...string) error {
	if s.Revision == "" {
		s.Revision = "main"
	}
	switch {
	case !repoRe.MatchString(s.Repo):
		return fmt.Errorf("repo %q must look like owner/name", s.Repo)
	case !pathRe.MatchString(s.File) || strings.Contains(s.File, ".."):
		return fmt.Errorf("file %q must be a path inside the repo", s.File)
	case !hasExt(s.File, exts):
		return fmt.Errorf("file %q must be a %s file", s.File, strings.Join(exts, " or "))
	case !revisionRe.MatchString(s.Revision):
		return fmt.Errorf("revision %q is invalid", s.Revision)
	}
	return nil
}

func hasExt(file string, exts []string) bool {
	for _, e := range exts {
		if len(file) > len(e) && strings.HasSuffix(file, e) {
			return true
		}
	}
	return false
}

// FileInfo is what Hugging Face reports about a file. Exactly one of SHA256 (LFS) and
// GitSHA1 (a small file kept in git, only with Source.AllowGitBlob) is set.
type FileInfo struct {
	Size    int64
	SHA256  string
	GitSHA1 string
}

// Progress is called with downloaded and total bytes.
type Progress func(done, total int64)

// Client talks to Hugging Face.
type Client struct {
	BaseURL string
	HTTP    *http.Client
	// Token is an optional access token (gated repos); read from HF_TOKEN by callers.
	Token string
}

func (c *Client) base() string {
	if c.BaseURL == "" {
		return DefaultBaseURL
	}
	return strings.TrimRight(c.BaseURL, "/")
}

func (c *Client) http() *http.Client {
	if c.HTTP != nil {
		return c.HTTP
	}
	return http.DefaultClient
}

func (c *Client) auth(r *http.Request) {
	if c.Token != "" {
		r.Header.Set("Authorization", "Bearer "+c.Token)
	}
	r.Header.Set("User-Agent", "topcoat-node")
}

// Info fetches size and SHA-256 of an LFS file.
func (c *Client) Info(ctx context.Context, src Source) (FileInfo, error) {
	u := fmt.Sprintf("%s/api/models/%s/paths-info/%s", c.base(), src.Repo, url.PathEscape(src.Revision))
	form := url.Values{"paths": {src.File}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, strings.NewReader(form.Encode()))
	if err != nil {
		return FileInfo{}, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	c.auth(req)
	resp, err := c.http().Do(req)
	if err != nil {
		return FileInfo{}, fmt.Errorf("hugging face metadata: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return FileInfo{}, fmt.Errorf("hugging face metadata for %s: HTTP %d (repo, revision or access wrong?)", src.Repo, resp.StatusCode)
	}
	var entries []struct {
		Type string `json:"type"`
		Path string `json:"path"`
		OID  string `json:"oid"`
		Size int64  `json:"size"`
		LFS  *struct {
			OID  string `json:"oid"`
			Size int64  `json:"size"`
		} `json:"lfs"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&entries); err != nil {
		return FileInfo{}, fmt.Errorf("hugging face metadata: %w", err)
	}
	for _, e := range entries {
		if e.Path != src.File {
			continue
		}
		if e.LFS != nil && sha256Re.MatchString(e.LFS.OID) {
			return FileInfo{Size: e.LFS.Size, SHA256: e.LFS.OID}, nil
		}
		if e.LFS == nil && src.AllowGitBlob && sha1Re.MatchString(e.OID) && e.Size >= 0 && e.Size <= MaxGitFileBytes {
			return FileInfo{Size: e.Size, GitSHA1: e.OID}, nil
		}
		return FileInfo{}, fmt.Errorf("%s has no SHA-256 (not an LFS file); refusing to download unverified", src.File)
	}
	return FileInfo{}, fmt.Errorf("file %s not found in %s@%s", src.File, src.Repo, src.Revision)
}

// ErrChecksum reports a SHA-256 mismatch after the download.
var ErrChecksum = errors.New("SHA-256 mismatch")

// Download fetches src into dest (via dest+".part"), resuming a previous
// partial download, and verifies the SHA-256. progress is throttled by the
// caller.
// The caller validates src (Validate or ValidateFile); Download checks repo, path and
// revision again.
func (c *Client) Download(ctx context.Context, src Source, dest string, progress Progress) (FileInfo, error) {
	if err := src.ValidateFile(""); err != nil {
		return FileInfo{}, err
	}
	info, err := c.Info(ctx, src)
	if err != nil {
		return FileInfo{}, err
	}
	part := dest + ".part"
	h, want := newHash(info)
	var have int64
	if f, err := os.Open(part); err == nil {
		have, err = io.Copy(h, f)
		_ = f.Close()
		if err != nil {
			return info, err
		}
	}
	if have > info.Size {
		_ = os.Remove(part)
		h, _ = newHash(info)
		have = 0
	}
	if have < info.Size {
		if err := c.fetch(ctx, src, part, &have, &h, info, progress); err != nil {
			return info, err
		}
	}
	if got := hex.EncodeToString(h.Sum(nil)); got != want {
		_ = os.Remove(part)
		return info, fmt.Errorf("%w for %s (got %s, expected %s); partial file removed", ErrChecksum, src.File, got, want)
	}
	if err := os.Rename(part, dest); err != nil {
		return info, err
	}
	if progress != nil {
		progress(info.Size, info.Size)
	}
	return info, nil
}

// newHash returns the hash the file is verified with and the expected hex digest. A git blob
// id hashes a "blob <size>\x00" header before the content.
func newHash(info FileInfo) (hash.Hash, string) {
	if info.SHA256 != "" {
		return sha256.New(), info.SHA256
	}
	h := sha1.New() //nolint:gosec // git blob id, see Source.AllowGitBlob
	fmt.Fprintf(h, "blob %d\x00", info.Size)
	return h, info.GitSHA1
}

func (c *Client) fetch(ctx context.Context, src Source, part string, have *int64, h *hash.Hash, info FileInfo, progress Progress) error {
	size := info.Size
	u := fmt.Sprintf("%s/%s/resolve/%s/%s", c.base(), src.Repo, url.PathEscape(src.Revision), src.File)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return err
	}
	c.auth(req)
	if *have > 0 {
		req.Header.Set("Range", "bytes="+strconv.FormatInt(*have, 10)+"-")
	}
	resp, err := c.http().Do(req)
	if err != nil {
		return fmt.Errorf("download %s: %w", src.File, err)
	}
	defer resp.Body.Close()
	flags := os.O_CREATE | os.O_WRONLY
	switch {
	case resp.StatusCode == http.StatusPartialContent && *have > 0:
		flags |= os.O_APPEND
	case resp.StatusCode == http.StatusOK:
		// Server ignored the range: start over.
		flags |= os.O_TRUNC
		*h, _ = newHash(info)
		*have = 0
	default:
		return fmt.Errorf("download %s: HTTP %d", src.File, resp.StatusCode)
	}
	f, err := os.OpenFile(part, flags, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	buf := make([]byte, 1<<20)
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			if _, err := f.Write(buf[:n]); err != nil {
				return err
			}
			_, _ = (*h).Write(buf[:n])
			*have += int64(n)
			if progress != nil {
				progress(*have, size)
			}
		}
		if errors.Is(rerr, io.EOF) {
			break
		}
		if rerr != nil {
			return fmt.Errorf("download %s: %w (resumable)", src.File, rerr)
		}
	}
	return f.Close()
}

// Throttle wraps p so that it is called at most every interval, plus always
// for the final call (done == total).
func Throttle(p Progress, interval time.Duration, now func() time.Time) Progress {
	var last time.Time
	return func(done, total int64) {
		t := now()
		if done < total && !last.IsZero() && t.Sub(last) < interval {
			return
		}
		last = t
		p(done, total)
	}
}
