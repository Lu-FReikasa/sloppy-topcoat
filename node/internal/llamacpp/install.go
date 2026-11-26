package llamacpp

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
)

// Installation is an unpacked, verified build.
type Installation struct {
	Release string `json:"release"`
	Backend string `json:"backend"`
	// Dir contains llama-server and llama-bench next to their libraries.
	Dir    string `json:"dir"`
	Server string `json:"server"`
	Bench  string `json:"bench"`
}

// Progress reports download progress over all assets of a build.
type Progress func(done, total int64)

// Installer downloads and unpacks builds into Root (<data dir>/runtime).
type Installer struct {
	Root     string
	Manifest Manifest
	// BaseURL overrides Manifest.BaseURL (tests).
	BaseURL string
	Client  *http.Client
	GOOS    string
	// Executables are the programs a build must contain (without .exe): the first is the server
	// (Installation.Server), the second, if any, the benchmark (Installation.Bench). Default:
	// llama-server, llama-bench. The stable-diffusion.cpp runner uses the same installer (D126).
	Executables []string

	mu sync.Mutex // one installation at a time
}

// NewInstaller returns an installer with the pinned manifest.
func NewInstaller(root string) *Installer {
	return &Installer{Root: root, Manifest: Pinned(), GOOS: runtime.GOOS}
}

const markerFile = "topcoat-install.json"

func (i *Installer) dir(release, backend string) string {
	return filepath.Join(i.Root, release+"-"+backend)
}

func exeName(goos, name string) string {
	if goos == "windows" {
		return name + ".exe"
	}
	return name
}

// Installed returns the installation if it is complete.
func (i *Installer) Installed(release, backend string) (Installation, bool) {
	b, err := os.ReadFile(filepath.Join(i.dir(release, backend), markerFile))
	if err != nil {
		return Installation{}, false
	}
	var inst Installation
	if json.Unmarshal(b, &inst) != nil || inst.Release != release || inst.Backend != backend {
		return Installation{}, false
	}
	if _, err := os.Stat(inst.Server); err != nil {
		return Installation{}, false
	}
	return inst, true
}

// Ensure installs release/backend unless it is already installed.
func (i *Installer) Ensure(ctx context.Context, release, backend string, progress Progress) (Installation, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if inst, ok := i.Installed(release, backend); ok {
		return inst, nil
	}
	build, err := i.Manifest.Build(release, i.goos(), backend)
	if err != nil {
		return Installation{}, err
	}
	if err := os.MkdirAll(i.Root, 0o750); err != nil {
		return Installation{}, err
	}
	dl := filepath.Join(i.Root, ".download")
	if err := os.MkdirAll(dl, 0o750); err != nil {
		return Installation{}, err
	}
	defer os.RemoveAll(dl) //nolint:errcheck // best effort cleanup
	tmp := i.dir(release, backend) + ".tmp"
	_ = os.RemoveAll(tmp)
	defer os.RemoveAll(tmp) //nolint:errcheck // best effort cleanup
	if err := os.MkdirAll(tmp, 0o750); err != nil {
		return Installation{}, err
	}

	var done int64
	for _, a := range build.Assets {
		path := filepath.Join(dl, a.Name)
		n, err := i.download(ctx, release, a, path, func(d, t int64) {
			if progress != nil {
				progress(done+d, done+t)
			}
		})
		if err != nil {
			return Installation{}, err
		}
		done += n
		if err := extract(path, tmp); err != nil {
			return Installation{}, fmt.Errorf("unpack %s: %w", a.Name, err)
		}
	}
	exes := i.Executables
	if len(exes) == 0 {
		exes = []string{"llama-server", "llama-bench"}
	}
	server, err := find(tmp, exeName(i.goos(), exes[0]))
	if err != nil {
		return Installation{}, err
	}
	bench := ""
	if len(exes) > 1 {
		if bench, err = find(tmp, exeName(i.goos(), exes[1])); err != nil {
			return Installation{}, err
		}
	}
	final := i.dir(release, backend)
	_ = os.RemoveAll(final)
	if err := os.Rename(tmp, final); err != nil {
		return Installation{}, err
	}
	rel := func(p string) string { r, _ := filepath.Rel(tmp, p); return filepath.Join(final, r) }
	inst := Installation{Release: release, Backend: backend, Server: rel(server)}
	if bench != "" {
		inst.Bench = rel(bench)
	}
	inst.Dir = filepath.Dir(inst.Server)
	b, _ := json.MarshalIndent(inst, "", "  ")
	if err := os.WriteFile(filepath.Join(final, markerFile), b, 0o600); err != nil {
		return Installation{}, err
	}
	return inst, nil
}

func (i *Installer) goos() string {
	if i.GOOS != "" {
		return i.GOOS
	}
	return runtime.GOOS
}

// download fetches one asset and verifies its SHA-256. Returns its size.
func (i *Installer) download(ctx context.Context, release string, a Asset, path string, progress Progress) (int64, error) {
	base := i.BaseURL
	if base == "" {
		base = i.Manifest.BaseURL
	}
	url := strings.TrimRight(base, "/") + "/" + release + "/" + a.Name
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return 0, err
	}
	client := i.Client
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, fmt.Errorf("download %s: %w", a.Name, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("download %s: HTTP %d", a.Name, resp.StatusCode)
	}
	f, err := os.Create(path)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	h := sha256.New()
	pr := &progressReader{r: resp.Body, total: resp.ContentLength, fn: progress}
	n, err := io.Copy(io.MultiWriter(f, h), pr)
	if err != nil {
		return 0, fmt.Errorf("download %s: %w", a.Name, err)
	}
	if got := hex.EncodeToString(h.Sum(nil)); got != a.SHA256 {
		return 0, fmt.Errorf("download %s: SHA-256 mismatch (got %s, pinned %s)", a.Name, got, a.SHA256)
	}
	return n, f.Close()
}

type progressReader struct {
	r     io.Reader
	done  int64
	total int64
	fn    Progress
}

func (p *progressReader) Read(b []byte) (int, error) {
	n, err := p.r.Read(b)
	p.done += int64(n)
	if p.fn != nil && n > 0 {
		p.fn(p.done, max(p.total, p.done))
	}
	return n, err
}

// find locates a file by name anywhere below root.
func find(root, name string) (string, error) {
	var found string
	err := filepath.WalkDir(root, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() && d.Name() == name {
			found = p
			return filepath.SkipAll
		}
		return nil
	})
	if err != nil {
		return "", err
	}
	if found == "" {
		return "", fmt.Errorf("%s not found in the llama.cpp archive", name)
	}
	return found, nil
}

// extract unpacks a .zip or .tar.gz into dest. If all entries of an archive
// share one top-level directory it is stripped, so that the CUDA runtime
// archive lands next to the binaries (they use $ORIGIN / the exe dir).
func extract(archive, dest string) error {
	if strings.HasSuffix(archive, ".zip") {
		return extractZip(archive, dest)
	}
	return extractTarGz(archive, dest)
}

// target resolves an archive path under dest, rejecting path traversal.
func target(dest, name, strip string) (string, bool, error) {
	name = strings.ReplaceAll(name, `\`, "/")
	name = strings.TrimPrefix(name, "./")
	if strip != "" {
		if name == strip || name == strip+"/" {
			return "", false, nil
		}
		name = strings.TrimPrefix(name, strip+"/")
	}
	if name == "" || name == "." {
		return "", false, nil
	}
	clean := filepath.Clean(filepath.FromSlash(name))
	if filepath.IsAbs(clean) || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", false, fmt.Errorf("archive entry %q escapes the target directory", name)
	}
	return filepath.Join(dest, clean), true, nil
}

func commonTop(names []string) string {
	top := ""
	for _, n := range names {
		n = strings.TrimPrefix(strings.ReplaceAll(n, `\`, "/"), "./")
		if n == "" || n == "." {
			continue
		}
		first, _, hasSlash := strings.Cut(n, "/")
		if !hasSlash && !strings.HasSuffix(n, "/") {
			return "" // a file at the top level
		}
		if top == "" {
			top = first
		} else if top != first {
			return ""
		}
	}
	return top
}

func extractZip(archive, dest string) error {
	z, err := zip.OpenReader(archive)
	if err != nil {
		return err
	}
	defer z.Close()
	names := make([]string, 0, len(z.File))
	for _, f := range z.File {
		names = append(names, f.Name)
	}
	strip := commonTop(names)
	for _, f := range z.File {
		p, ok, err := target(dest, f.Name, strip)
		if err != nil {
			return err
		}
		if !ok {
			continue
		}
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(p, 0o750); err != nil {
				return err
			}
			continue
		}
		if f.Mode()&os.ModeSymlink != 0 {
			continue // not used by the Windows archives
		}
		rc, err := f.Open()
		if err != nil {
			return err
		}
		err = writeFile(p, rc, f.Mode()|0o640)
		_ = rc.Close()
		if err != nil {
			return err
		}
	}
	return nil
}

func extractTarGz(archive, dest string) error {
	names, err := tarNames(archive)
	if err != nil {
		return err
	}
	strip := commonTop(names)
	f, err := os.Open(archive)
	if err != nil {
		return err
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return err
	}
	tr := tar.NewReader(gz)
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		p, ok, err := target(dest, h.Name, strip)
		if err != nil {
			return err
		}
		if !ok {
			continue
		}
		switch h.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(p, 0o750); err != nil {
				return err
			}
		case tar.TypeReg:
			if err := writeFile(p, tr, os.FileMode(h.Mode&0o750)|0o640); err != nil {
				return err
			}
		case tar.TypeSymlink:
			// Shared-library version links (libfoo.so -> libfoo.so.0). Only
			// plain names in the same directory are accepted.
			if strings.ContainsAny(h.Linkname, `/\`) || h.Linkname == ".." {
				return fmt.Errorf("archive symlink %q -> %q not allowed", h.Name, h.Linkname)
			}
			if err := os.MkdirAll(filepath.Dir(p), 0o750); err != nil {
				return err
			}
			_ = os.Remove(p)
			if err := os.Symlink(h.Linkname, p); err != nil {
				return err
			}
		}
	}
}

func tarNames(archive string) ([]string, error) {
	f, err := os.Open(archive)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return nil, err
	}
	tr := tar.NewReader(gz)
	var names []string
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			return names, nil
		}
		if err != nil {
			return nil, err
		}
		n := h.Name
		if h.Typeflag == tar.TypeDir && !strings.HasSuffix(n, "/") {
			n += "/"
		}
		names = append(names, n)
	}
}

func writeFile(p string, r io.Reader, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(p), 0o750); err != nil {
		return err
	}
	f, err := os.OpenFile(p, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode.Perm())
	if err != nil {
		return err
	}
	if _, err := io.Copy(f, r); err != nil {
		_ = f.Close()
		return err
	}
	return f.Close()
}
