package update

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"
)

// File names inside <data dir>/update. The directory belongs to the node's service account; the
// updater (privileged) reads and removes some of the files, so nothing in here is trusted until
// the updater has verified the bytes it read.
const (
	dirName       = "update"
	stagingName   = "staging"
	readyName     = "ready"
	applyingName  = "applying.json"
	confirmedName = "confirmed.json"
	resultName    = "result.json"
	statusName    = "status.json"
	reportedName  = "reported.json"

	// ManifestFile, SigFile and BinaryFile are the files of a staged bundle.
	ManifestFile = "manifest.json"
	SigFile      = "manifest.sig"
	BinaryFile   = "topcoat-node"
)

// Layout is the update directories of one node.
//
// Dir belongs to the node's service account: it stages bundles there and the privileged updater
// reads them. State is written by the updater (its marker, heartbeat and result) and must belong
// to the updater's account: if the node could prepare links in a directory that root writes into,
// the node could make root create or overwrite files elsewhere. Without a separate State (tests,
// development) both are the same directory.
type Layout struct{ Dir, State string }

// NewLayout returns the layout below a node data directory.
func NewLayout(dataDir string) Layout {
	d := filepath.Join(dataDir, dirName)
	return Layout{Dir: d, State: d}
}

// WithState returns the layout with the updater's own state directory; "" keeps it as it is.
func (l Layout) WithState(dir string) Layout {
	if dir != "" {
		l.State = dir
	}
	return l
}

func (l Layout) stateDir() string {
	if l.State == "" {
		return l.Dir
	}
	return l.State
}

// Staging is where the node puts the bundle it has downloaded and checked.
func (l Layout) Staging() string { return filepath.Join(l.Dir, stagingName) }

func (l Layout) ready() string     { return filepath.Join(l.Dir, readyName) }
func (l Layout) applying() string  { return filepath.Join(l.stateDir(), applyingName) }
func (l Layout) confirmed() string { return filepath.Join(l.Dir, confirmedName) }
func (l Layout) result() string    { return filepath.Join(l.stateDir(), resultName) }
func (l Layout) status() string    { return filepath.Join(l.stateDir(), statusName) }
func (l Layout) reported() string  { return filepath.Join(l.Dir, reportedName) }

// EnsureDir creates the update directory (private to the service account). The state directory is
// only checked: the installer creates it with the right owner, and an updater that created it
// itself would hand the node a directory it cannot write into.
func (l Layout) EnsureDir() error {
	if err := os.MkdirAll(l.Dir, 0o700); err != nil {
		return err
	}
	return l.CheckDirs()
}

// CheckDirs refuses directories that are links: the privileged updater must not be led somewhere
// else by something the node account prepared.
func (l Layout) CheckDirs() error {
	for _, d := range []string{l.Dir, l.stateDir()} {
		fi, err := os.Lstat(d)
		if err != nil {
			return err
		}
		if !fi.IsDir() {
			return fmt.Errorf("%s is not a plain directory", d)
		}
	}
	return nil
}

// Ready is the node's request to the updater: the bundle in Staging is complete.
type Ready struct {
	Version  string `json:"version"`
	StagedAt string `json:"staged_at"`
}

// Applying is the updater's own marker while an update is in progress (crash recovery).
type Applying struct {
	Version string `json:"version"`
	// Phase is "stopping" until the program file was replaced, then "replaced".
	Phase     string `json:"phase"`
	StartedAt string `json:"started_at"`
}

// Phases of an update in progress.
const (
	PhaseStopping = "stopping"
	PhaseReplaced = "replaced"
)

// Confirmed is what a started node writes once the hub accepted it.
type Confirmed struct {
	Version string `json:"version"`
	At      string `json:"at"`
}

// Result is what the updater leaves for the node to report.
type Result struct {
	// ID tells two results apart: the node cannot delete the result (the state directory is the
	// updater's), so it remembers the last one it reported.
	ID      string `json:"id,omitempty"`
	OK      bool   `json:"ok"`
	Version string `json:"version,omitempty"`
	Reason  string `json:"reason,omitempty"`
	At      string `json:"at"`
}

// Status is the updater's heartbeat.
type Status struct {
	AliveAt string   `json:"alive_at"`
	Version string   `json:"version"`
	Keys    []string `json:"keys"`
}

func stamp(t time.Time) string { return t.UTC().Format(time.RFC3339) }

// writeAtomic writes data to path through a temporary file in the same directory.
func writeAtomic(path string, data []byte, mode os.FileMode) error {
	// The updater runs with more rights than the account that owns this directory, so a link
	// planted at the temporary name must not be followed: remove it, then create exclusively.
	tmp := path + ".tmp"
	_ = os.Remove(tmp)
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode) //nolint:gosec // see above
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return err
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}

func writeJSON(path string, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return writeAtomic(path, b, 0o644) //nolint:gosec // the node (another account) must be able to read it
}

// readRegular reads a regular file of at most limit bytes. Links are refused: a file in the
// update directory is written by the service account, and the updater runs with more rights.
func readRegular(path string, limit int64) ([]byte, error) {
	li, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !li.Mode().IsRegular() {
		return nil, fmt.Errorf("%s is not a regular file", filepath.Base(path))
	}
	f, err := os.Open(path) //nolint:gosec // checked with Lstat/SameFile below
	if err != nil {
		return nil, err
	}
	defer f.Close() //nolint:errcheck // read only
	fi, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !os.SameFile(li, fi) {
		return nil, fmt.Errorf("%s changed while it was opened", filepath.Base(path))
	}
	if fi.Size() > limit {
		return nil, fmt.Errorf("%s is %d bytes, more than the limit of %d", filepath.Base(path), fi.Size(), limit)
	}
	return io.ReadAll(io.LimitReader(f, limit+1))
}

func readJSON(path string, v any) error {
	b, err := readRegular(path, MaxManifestBytes)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, v)
}

func removeIfExists(path string) {
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		_ = os.RemoveAll(path)
	}
}

// ReadStaged reads the staged bundle: manifest, signature line and program bytes.
func (l Layout) ReadStaged() (manifest []byte, sigLine string, binary []byte, err error) {
	dir := l.Staging()
	if manifest, err = readRegular(filepath.Join(dir, ManifestFile), MaxManifestBytes); err != nil {
		return nil, "", nil, err
	}
	sig, err := readRegular(filepath.Join(dir, SigFile), MaxManifestBytes)
	if err != nil {
		return nil, "", nil, err
	}
	if binary, err = readRegular(filepath.Join(dir, BinaryFile), MaxBinaryBytes); err != nil {
		return nil, "", nil, err
	}
	return manifest, string(sig), binary, nil
}

// WriteConfirmed records that the running node (this version) was accepted by the hub.
func (l Layout) WriteConfirmed(version string, now time.Time) error {
	if err := l.EnsureDir(); err != nil {
		return err
	}
	return writeJSON(l.confirmed(), Confirmed{Version: version, At: stamp(now)})
}

// ClearConfirmed removes a confirmation (before a bundle is staged).
func (l Layout) ClearConfirmed() { removeIfExists(l.confirmed()) }

func (r Result) key() string {
	if r.ID != "" {
		return r.ID
	}
	return r.At + "|" + r.Version + "|" + r.Reason
}

// TakeResult returns the updater's result once: nil when there is none or the node reported this
// one already. It is remembered in the node's own directory, so a result that stays in the
// updater's directory is not reported again at every start.
func (l Layout) TakeResult() *Result {
	var r Result
	if err := readJSON(l.result(), &r); err != nil {
		return nil
	}
	var done struct {
		Key string `json:"key"`
	}
	if err := readJSON(l.reported(), &done); err == nil && done.Key == r.key() {
		return nil
	}
	if err := l.EnsureDir(); err == nil {
		_ = writeJSON(l.reported(), struct {
			Key string `json:"key"`
		}{r.key()})
	}
	return &r
}

// ReadStatus returns the updater's heartbeat; nil when it never wrote one.
func (l Layout) ReadStatus() *Status {
	var s Status
	if err := readJSON(l.status(), &s); err != nil {
		return nil
	}
	return &s
}

// MarkReady tells the updater that the staged bundle is complete.
func (l Layout) MarkReady(version string, now time.Time) error {
	return writeJSON(l.ready(), Ready{Version: version, StagedAt: stamp(now)})
}

// Clear removes the staged bundle and the request (not the result).
func (l Layout) Clear() {
	removeIfExists(l.ready())
	removeIfExists(l.Staging())
}
