package update

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

var plat = Platform{OS: "linux", Arch: "amd64"}

type testKeys struct {
	priv ed25519.PrivateKey
	pub  ed25519.PublicKey
	keys []Key
}

func newKeys(t *testing.T) testKeys {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	keys, err := ParseKeys(base64.StdEncoding.EncodeToString(pub))
	if err != nil {
		t.Fatal(err)
	}
	return testKeys{priv: priv, pub: pub, keys: keys}
}

// bundle builds a signed bundle for the tests.
type bundle struct {
	manifest []byte
	sig      string
	binary   []byte
}

func (k testKeys) bundle(t *testing.T, mutate func(*Manifest)) bundle {
	t.Helper()
	binary := []byte("#!/bin/sh\necho new node\n")
	m := Manifest{Format: 1, Product: Product, Channel: "stable", Version: "0.2.0", OS: "linux", Arch: "amd64",
		Size: int64(len(binary)), SHA256: SHA256Hex(binary), Created: "2026-10-07T20:00:00Z"}
	if mutate != nil {
		mutate(&m)
	}
	b, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return bundle{manifest: b, sig: Sign(k.priv, b), binary: binary}
}

func TestParseKeys(t *testing.T) {
	k := newKeys(t)
	if len(k.keys) != 1 || k.keys[0].ID != KeyID(k.pub) || len(k.keys[0].ID) != 16 {
		t.Fatalf("keys = %+v", k.keys)
	}
	if ks, err := ParseKeys(""); err != nil || len(ks) != 0 {
		t.Fatalf("empty = %v, %v", ks, err)
	}
	if _, err := ParseKeys("not base64!"); err == nil {
		t.Fatal("garbage accepted")
	}
	if _, err := ParseKeys(base64.StdEncoding.EncodeToString([]byte("short"))); err == nil {
		t.Fatal("short key accepted")
	}
	a, b, c := newKeys(t), newKeys(t), newKeys(t)
	three := strings.Join([]string{
		base64.StdEncoding.EncodeToString(a.pub), base64.StdEncoding.EncodeToString(b.pub), base64.StdEncoding.EncodeToString(c.pub)}, ",")
	if _, err := ParseKeys(three); err == nil {
		t.Fatal("three keys accepted")
	}
	two, err := ParseKeys(base64.StdEncoding.EncodeToString(a.pub) + " , " + base64.StdEncoding.EncodeToString(b.pub))
	if err != nil || len(two) != 2 {
		t.Fatalf("two keys: %v %v", two, err)
	}
}

func TestVerify(t *testing.T) {
	k := newKeys(t)
	b := k.bundle(t, nil)
	if err := Verify(k.keys, b.manifest, b.sig); err != nil {
		t.Fatalf("good signature: %v", err)
	}
	tampered := bytes.Replace(b.manifest, []byte("0.2.0"), []byte("9.9.9"), 1)
	if err := Verify(k.keys, tampered, b.sig); err == nil {
		t.Fatal("tampered manifest verified")
	}
	other := newKeys(t)
	if err := Verify(other.keys, b.manifest, b.sig); err == nil || !strings.Contains(err.Error(), "not trusted") {
		t.Fatalf("foreign key: %v", err)
	}
	if err := Verify(nil, b.manifest, b.sig); !errors.Is(err, ErrNoKeys) {
		t.Fatalf("no keys: %v", err)
	}
	for _, bad := range []string{"", "topcoat-sig-v1", "other-v9 " + k.keys[0].ID + " AAAA", "topcoat-sig-v1 " + k.keys[0].ID + " !!!"} {
		if err := Verify(k.keys, b.manifest, bad); err == nil {
			t.Fatalf("signature line %q accepted", bad)
		}
	}
	// A reserve key also verifies.
	reserve := newKeys(t)
	both := append(append([]Key{}, k.keys...), reserve.keys...)
	rb := reserve.bundle(t, nil)
	if err := Verify(both, rb.manifest, rb.sig); err != nil {
		t.Fatalf("reserve key: %v", err)
	}
}

func TestSignatureCoversTheDomainPrefix(t *testing.T) {
	k := newKeys(t)
	b := k.bundle(t, nil)
	// A signature over the bare manifest (no domain prefix) must not verify.
	raw := ed25519.Sign(k.priv, b.manifest)
	line := SigScheme + " " + k.keys[0].ID + " " + base64.StdEncoding.EncodeToString(raw)
	if err := Verify(k.keys, b.manifest, line); err == nil {
		t.Fatal("signature without the domain prefix verified")
	}
}

func TestParseManifestIsStrict(t *testing.T) {
	k := newKeys(t)
	good := k.bundle(t, nil).manifest
	if _, err := ParseManifest(good); err != nil {
		t.Fatal(err)
	}
	cases := map[string][]byte{
		"unknown field": bytes.Replace(good, []byte(`"format":1`), []byte(`"format":1,"extra":true`), 1),
		"trailing":      append(append([]byte{}, good...), []byte(`{}`)...),
		"empty":         nil,
		"not json":      []byte("version 1"),
	}
	for name, b := range cases {
		if _, err := ParseManifest(b); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
	mutations := map[string]func(*Manifest){
		"format":    func(m *Manifest) { m.Format = 2 },
		"product":   func(m *Manifest) { m.Product = "other" },
		"channel":   func(m *Manifest) { m.Channel = "../x" },
		"version":   func(m *Manifest) { m.Version = "1.2" },
		"os":        func(m *Manifest) { m.OS = "Linux/../" },
		"size":      func(m *Manifest) { m.Size = 0 },
		"too big":   func(m *Manifest) { m.Size = MaxBinaryBytes + 1 },
		"sha upper": func(m *Manifest) { m.SHA256 = strings.ToUpper(m.SHA256) },
	}
	for name, mut := range mutations {
		if _, err := ParseManifest(k.bundle(t, mut).manifest); err == nil {
			t.Errorf("manifest with bad %s accepted", name)
		}
	}
}

func TestVersions(t *testing.T) {
	cases := []struct {
		cand, inst string
		newer      bool
	}{
		{"0.2.0", "0.1.0", true},
		{"0.1.0", "0.1.0", false},
		{"0.1.0", "0.2.0", false},
		{"1.0.0", "0.9.9", true},
		{"0.10.0", "0.9.0", true}, // numeric, not lexical
		{"0.1.0", "0.1.0-dev", true},
		{"0.1.0-rc1", "0.1.0", false},
		{"0.1.0-rc2", "0.1.0-rc1", true},
		{"0.0.1", "dev-build", true}, // an unparsable installed version counts as 0.0.0
	}
	for _, c := range cases {
		got, err := Newer(c.cand, c.inst)
		if err != nil || got != c.newer {
			t.Errorf("Newer(%s, %s) = %v, %v; want %v", c.cand, c.inst, got, err, c.newer)
		}
	}
	if _, err := Newer("banana", "0.1.0"); err == nil {
		t.Fatal("unparsable candidate accepted")
	}
}

func TestCheck(t *testing.T) {
	k := newKeys(t)
	b := k.bundle(t, nil)
	if _, err := Check(k.keys, b.manifest, b.sig, b.binary, plat, "0.1.0"); err != nil {
		t.Fatalf("good bundle: %v", err)
	}
	if _, err := Check(k.keys, b.manifest, b.sig, b.binary, Platform{"windows", "amd64"}, "0.1.0"); err == nil {
		t.Fatal("wrong platform accepted")
	}
	if _, err := Check(k.keys, b.manifest, b.sig, b.binary, plat, "0.2.0"); err == nil {
		t.Fatal("same version accepted")
	}
	if _, err := Check(k.keys, b.manifest, b.sig, b.binary, plat, "0.3.0"); err == nil {
		t.Fatal("downgrade accepted")
	}
	if _, err := Check(k.keys, b.manifest, b.sig, append([]byte("x"), b.binary[1:]...), plat, "0.1.0"); err == nil {
		t.Fatal("changed program accepted")
	}
	if _, err := Check(k.keys, b.manifest, b.sig, b.binary[:5], plat, "0.1.0"); err == nil {
		t.Fatal("short program accepted")
	}
}

// fakeSource serves a bundle.
type fakeSource struct {
	b        bundle
	sizeLie  bool
	binErr   error
	manErr   error
	requests []string
}

func (f *fakeSource) Manifest(_ context.Context, version string) ([]byte, string, error) {
	f.requests = append(f.requests, "manifest:"+version)
	return f.b.manifest, f.b.sig, f.manErr
}

func (f *fakeSource) Binary(_ context.Context, version string, dst io.Writer, limit int64) error {
	f.requests = append(f.requests, "binary:"+version)
	if f.binErr != nil {
		_, _ = dst.Write(f.b.binary[:3])
		return f.binErr
	}
	data := f.b.binary
	if f.sizeLie {
		data = append(append([]byte{}, data...), make([]byte, 100)...)
	}
	if int64(len(data)) > limit {
		data = data[:limit]
	}
	_, err := dst.Write(data)
	return err
}

func newStager(t *testing.T, k testKeys, src Source) *Stager {
	t.Helper()
	return &Stager{Keys: k.keys, Layout: NewLayout(t.TempDir()), Source: src, Plat: plat, Installed: "0.1.0",
		Channel: "stable", Now: func() time.Time { return time.Date(2026, 10, 7, 20, 0, 0, 0, time.UTC) }}
}

func TestStage(t *testing.T) {
	k := newKeys(t)
	src := &fakeSource{b: k.bundle(t, nil)}
	s := newStager(t, k, src)
	m, err := s.Stage(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	if m.Version != "0.2.0" {
		t.Fatalf("version = %s", m.Version)
	}
	man, sig, bin, err := s.Layout.ReadStaged()
	if err != nil || !bytes.Equal(man, src.b.manifest) || sig != src.b.sig || !bytes.Equal(bin, src.b.binary) {
		t.Fatalf("staged bundle differs: %v", err)
	}
	var r Ready
	if err := readJSON(s.Layout.ready(), &r); err != nil || r.Version != "0.2.0" {
		t.Fatalf("ready = %+v, %v", r, err)
	}
}

func TestStageRefusals(t *testing.T) {
	k := newKeys(t)
	cases := map[string]func(*fakeSource, *Stager){
		"wrong channel":  func(f *fakeSource, s *Stager) { s.Channel = "test" },
		"not newer":      func(f *fakeSource, s *Stager) { s.Installed = "0.2.0" },
		"other platform": func(f *fakeSource, s *Stager) { s.Plat = Platform{"windows", "amd64"} },
		"no keys":        func(f *fakeSource, s *Stager) { s.Keys = nil },
		"foreign key":    func(f *fakeSource, s *Stager) { s.Keys = newKeys(t).keys },
		"wrong version":  func(f *fakeSource, s *Stager) { f.b = k.bundle(t, func(m *Manifest) { m.Version = "0.3.0" }) },
		"size lie":       func(f *fakeSource, s *Stager) { f.sizeLie = true },
		"download error": func(f *fakeSource, s *Stager) { f.binErr = errors.New("connection reset") },
		"manifest error": func(f *fakeSource, s *Stager) { f.manErr = errors.New("HTTP 500") },
		"bad hash": func(f *fakeSource, s *Stager) {
			f.b = k.bundle(t, func(m *Manifest) { m.SHA256 = SHA256Hex([]byte("something else")) })
		},
	}
	for name, setup := range cases {
		t.Run(name, func(t *testing.T) {
			src := &fakeSource{b: k.bundle(t, nil)}
			s := newStager(t, k, src)
			setup(src, s)
			ask := ""
			if name == "wrong version" {
				ask = "0.2.0"
			}
			if _, err := s.Stage(context.Background(), ask); err == nil {
				t.Fatal("staged")
			}
			if _, err := os.Stat(s.Layout.ready()); err == nil {
				t.Error("a refused build left a ready request")
			}
			if _, err := os.Stat(s.Layout.Staging()); err == nil {
				t.Error("a refused build left a staging directory")
			}
		})
	}
}

func TestStageReplacesAnEarlierBundleAndClearsConfirmation(t *testing.T) {
	k := newKeys(t)
	src := &fakeSource{b: k.bundle(t, nil)}
	s := newStager(t, k, src)
	if err := s.Layout.WriteConfirmed("0.2.0", time.Now()); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Stage(context.Background(), ""); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(s.Layout.confirmed()); err == nil {
		t.Fatal("an old confirmation survived staging")
	}
	if _, err := s.Stage(context.Background(), ""); err != nil {
		t.Fatalf("staging twice: %v", err)
	}
}

// fakeHost records what the updater does to the machine.
type fakeHost struct {
	t        *testing.T
	layout   Layout
	program  []byte
	kept     []byte
	hasKept  bool
	running  bool
	log      []string
	stopErr  error
	repErr   error
	startErr map[int]error // by start call number
	starts   int
	// onStart is called when the node starts with the current program, like the real node writing
	// its confirmation.
	onStart func(program []byte)
}

func (h *fakeHost) StopNode(context.Context) error {
	h.log = append(h.log, "stop")
	if h.stopErr != nil {
		return h.stopErr
	}
	h.running = false
	return nil
}

func (h *fakeHost) StartNode(context.Context) error {
	h.starts++
	h.log = append(h.log, "start")
	if err := h.startErr[h.starts]; err != nil {
		return err
	}
	h.running = true
	if h.onStart != nil {
		h.onStart(h.program)
	}
	return nil
}

func (h *fakeHost) ReplaceProgram(_ context.Context, data []byte) error {
	h.log = append(h.log, "replace")
	h.hasKept = false
	if h.repErr != nil {
		return h.repErr
	}
	h.kept, h.hasKept = h.program, true
	h.program = data
	return nil
}

func (h *fakeHost) RestoreProgram(context.Context) error {
	h.log = append(h.log, "restore")
	if h.hasKept {
		h.program, h.hasKept = h.kept, false
	}
	return nil
}

func newUpdater(t *testing.T, k testKeys) (*Updater, *fakeHost) {
	t.Helper()
	layout := NewLayout(t.TempDir())
	if err := layout.EnsureDir(); err != nil {
		t.Fatal(err)
	}
	h := &fakeHost{t: t, layout: layout, program: []byte("old program"), running: true}
	// The "new node" confirms the version it carries: the fake program is "NODE <version>".
	h.onStart = func(program []byte) {
		if v, ok := strings.CutPrefix(string(program), "NODE "); ok {
			_ = layout.WriteConfirmed(v, time.Now())
		}
	}
	u := &Updater{Keys: k.keys, Layout: layout, Host: h, Plat: plat, Installed: "0.1.0",
		ConfirmTimeout: 50 * time.Millisecond, Poll: time.Millisecond}
	return u, h
}

// stage writes a bundle straight into the staging directory (what the Stager does) with a program
// that the fake host understands.
func stage(t *testing.T, u *Updater, k testKeys, mutate func(*Manifest), binary []byte) {
	t.Helper()
	if binary == nil {
		binary = []byte("NODE 0.2.0")
	}
	m := Manifest{Format: 1, Product: Product, Channel: "stable", Version: "0.2.0", OS: "linux", Arch: "amd64",
		Size: int64(len(binary)), SHA256: SHA256Hex(binary), Created: "2026-10-07T20:00:00Z"}
	if mutate != nil {
		mutate(&m)
	}
	mb, _ := json.Marshal(m)
	dir := u.Layout.Staging()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	for name, data := range map[string][]byte{ManifestFile: mb, SigFile: []byte(Sign(k.priv, mb)), BinaryFile: binary} {
		if err := os.WriteFile(filepath.Join(dir, name), data, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := u.Layout.MarkReady(m.Version, time.Now()); err != nil {
		t.Fatal(err)
	}
}

func result(t *testing.T, u *Updater) Result {
	t.Helper()
	r := u.Layout.TakeResult()
	if r == nil {
		t.Fatal("no result")
	}
	return *r
}

func TestApplyNothing(t *testing.T) {
	k := newKeys(t)
	u, h := newUpdater(t, k)
	out, err := u.Apply(context.Background())
	if out != NothingToDo || err != nil || len(h.log) != 0 {
		t.Fatalf("out=%v err=%v log=%v", out, err, h.log)
	}
}

func TestApplySuccess(t *testing.T) {
	k := newKeys(t)
	u, h := newUpdater(t, k)
	stage(t, u, k, nil, nil)
	out, err := u.Apply(context.Background())
	if out != Applied || err != nil {
		t.Fatalf("out=%v err=%v", out, err)
	}
	if string(h.program) != "NODE 0.2.0" || !h.running {
		t.Fatalf("program=%q running=%v", h.program, h.running)
	}
	if got := strings.Join(h.log, ","); got != "stop,replace,start" {
		t.Fatalf("host calls = %s", got)
	}
	if r := result(t, u); !r.OK || r.Version != "0.2.0" {
		t.Fatalf("result = %+v", r)
	}
	for _, p := range []string{u.Layout.ready(), u.Layout.applying(), u.Layout.Staging()} {
		if _, err := os.Stat(p); err == nil {
			t.Errorf("%s was left behind", p)
		}
	}
}

func TestApplyRefusals(t *testing.T) {
	k := newKeys(t)
	cases := map[string]func(*testing.T, *Updater){
		"foreign signature": func(t *testing.T, u *Updater) {
			stage(t, u, newKeys(t), nil, nil)
		},
		"downgrade": func(t *testing.T, u *Updater) {
			u.Installed = "0.3.0"
			stage(t, u, k, nil, nil)
		},
		"same version": func(t *testing.T, u *Updater) {
			u.Installed = "0.2.0"
			stage(t, u, k, nil, nil)
		},
		"other platform": func(t *testing.T, u *Updater) {
			stage(t, u, k, func(m *Manifest) { m.OS = "windows" }, nil)
		},
		"program changed after signing": func(t *testing.T, u *Updater) {
			stage(t, u, k, nil, nil)
			if err := os.WriteFile(filepath.Join(u.Layout.Staging(), BinaryFile), []byte("EVIL PROGRAM"), 0o600); err != nil {
				t.Fatal(err)
			}
		},
		"missing program": func(t *testing.T, u *Updater) {
			stage(t, u, k, nil, nil)
			_ = os.Remove(filepath.Join(u.Layout.Staging(), BinaryFile))
		},
		"no keys": func(t *testing.T, u *Updater) {
			u.Keys = nil
			stage(t, u, k, nil, nil)
		},
	}
	for name, setup := range cases {
		t.Run(name, func(t *testing.T) {
			u, h := newUpdater(t, k)
			setup(t, u)
			out, err := u.Apply(context.Background())
			if out != Failed || err == nil {
				t.Fatalf("out=%v err=%v", out, err)
			}
			if len(h.log) != 0 || string(h.program) != "old program" {
				t.Fatalf("a refused build touched the machine: log=%v program=%q", h.log, h.program)
			}
			if r := result(t, u); r.OK || r.Reason == "" {
				t.Fatalf("result = %+v", r)
			}
			if _, err := os.Stat(u.Layout.ready()); err == nil {
				t.Error("request not consumed")
			}
		})
	}
}

func TestApplyRefusesLinks(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("creating symlinks needs privileges on Windows")
	}
	k := newKeys(t)
	u, h := newUpdater(t, k)
	stage(t, u, k, nil, nil)
	target := filepath.Join(t.TempDir(), "elsewhere")
	if err := os.WriteFile(target, []byte("NODE 0.2.0"), 0o600); err != nil {
		t.Fatal(err)
	}
	bin := filepath.Join(u.Layout.Staging(), BinaryFile)
	_ = os.Remove(bin)
	if err := os.Symlink(target, bin); err != nil {
		t.Fatal(err)
	}
	if out, _ := u.Apply(context.Background()); out != Failed || len(h.log) != 0 {
		t.Fatalf("symlinked program: out=%v log=%v", out, h.log)
	}
}

func TestApplyRollsBackWhenTheNewNodeDoesNotConfirm(t *testing.T) {
	k := newKeys(t)
	u, h := newUpdater(t, k)
	h.onStart = nil // the new program never reports back
	stage(t, u, k, nil, nil)
	out, err := u.Apply(context.Background())
	if out != Failed || err == nil {
		t.Fatalf("out=%v err=%v", out, err)
	}
	if string(h.program) != "old program" || !h.running {
		t.Fatalf("program=%q running=%v", h.program, h.running)
	}
	if got := strings.Join(h.log, ","); got != "stop,replace,start,stop,restore,start" {
		t.Fatalf("host calls = %s", got)
	}
	if r := result(t, u); r.OK || !strings.Contains(r.Reason, "did not report back") {
		t.Fatalf("result = %+v", r)
	}
	if _, err := os.Stat(u.Layout.applying()); err == nil {
		t.Error("marker left behind")
	}
}

func TestApplyRollsBackOnAWrongVersionConfirmation(t *testing.T) {
	k := newKeys(t)
	u, h := newUpdater(t, k)
	h.onStart = func([]byte) { _ = u.Layout.WriteConfirmed("0.1.0", time.Now()) } // the old version came back
	stage(t, u, k, nil, nil)
	if out, _ := u.Apply(context.Background()); out != Failed {
		t.Fatalf("out=%v", out)
	}
	if r := result(t, u); !strings.Contains(r.Reason, "expected 0.2.0") {
		t.Fatalf("result = %+v", r)
	}
	if string(h.program) != "old program" {
		t.Fatalf("program=%q", h.program)
	}
}

func TestApplyRollsBackWhenTheNewNodeCannotStart(t *testing.T) {
	k := newKeys(t)
	u, h := newUpdater(t, k)
	h.startErr = map[int]error{1: errors.New("service failed to start")}
	stage(t, u, k, nil, nil)
	if out, _ := u.Apply(context.Background()); out != Failed {
		t.Fatalf("out=%v", out)
	}
	if string(h.program) != "old program" || !h.running {
		t.Fatalf("program=%q running=%v", h.program, h.running)
	}
}

func TestApplyStopFailureLeavesTheProgramAlone(t *testing.T) {
	k := newKeys(t)
	u, h := newUpdater(t, k)
	h.stopErr = errors.New("access denied")
	stage(t, u, k, nil, nil)
	if out, _ := u.Apply(context.Background()); out != Failed {
		t.Fatalf("out=%v", out)
	}
	if string(h.program) != "old program" {
		t.Fatalf("program=%q", h.program)
	}
	for _, c := range h.log {
		if c == "replace" || c == "restore" {
			t.Fatalf("host calls = %v", h.log)
		}
	}
}

func TestApplyReplaceFailureRestores(t *testing.T) {
	k := newKeys(t)
	u, h := newUpdater(t, k)
	h.repErr = errors.New("disk full")
	stage(t, u, k, nil, nil)
	if out, _ := u.Apply(context.Background()); out != Failed {
		t.Fatalf("out=%v", out)
	}
	if string(h.program) != "old program" || !h.running {
		t.Fatalf("program=%q running=%v", h.program, h.running)
	}
}

func TestRecoverBeforeReplacingDoesNotRestoreAnOldKeptProgram(t *testing.T) {
	k := newKeys(t)
	u, h := newUpdater(t, k)
	h.kept, h.hasKept = []byte("program of an older update"), true
	h.running = false
	if err := writeJSON(u.Layout.applying(), Applying{Version: "0.2.0", Phase: PhaseStopping}); err != nil {
		t.Fatal(err)
	}
	if out := u.recover(context.Background()); out != Failed {
		t.Fatalf("out=%v", out)
	}
	if string(h.program) != "old program" || !h.running {
		t.Fatalf("program=%q running=%v", h.program, h.running)
	}
	for _, c := range h.log {
		if c == "restore" {
			t.Fatalf("a stale program was restored: %v", h.log)
		}
	}
	if r := result(t, u); r.OK {
		t.Fatalf("result = %+v", r)
	}
}

func TestRecoverAfterReplacingConfirmsOrRollsBack(t *testing.T) {
	k := newKeys(t)
	t.Run("new node confirms", func(t *testing.T) {
		u, h := newUpdater(t, k)
		h.program = []byte("NODE 0.2.0")
		h.kept, h.hasKept = []byte("old program"), true
		_ = writeJSON(u.Layout.applying(), Applying{Version: "0.2.0", Phase: PhaseReplaced})
		if out := u.recover(context.Background()); out != Applied {
			t.Fatalf("out=%v", out)
		}
		if r := result(t, u); !r.OK {
			t.Fatalf("result = %+v", r)
		}
	})
	t.Run("new node is silent", func(t *testing.T) {
		u, h := newUpdater(t, k)
		h.onStart = nil
		h.program = []byte("NODE 0.2.0")
		h.kept, h.hasKept = []byte("old program"), true
		_ = writeJSON(u.Layout.applying(), Applying{Version: "0.2.0", Phase: PhaseReplaced})
		if out := u.recover(context.Background()); out != Failed {
			t.Fatalf("out=%v", out)
		}
		if string(h.program) != "old program" {
			t.Fatalf("program=%q", h.program)
		}
	})
}

func TestRunWritesStatusAndStopsAfterAnUpdate(t *testing.T) {
	k := newKeys(t)
	u, _ := newUpdater(t, k)
	u.Now = func() time.Time { return time.Date(2026, 10, 7, 20, 0, 0, 0, time.UTC) }
	stage(t, u, k, nil, nil)
	err := u.Run(context.Background())
	if !errors.Is(err, ErrApplied) {
		t.Fatalf("Run = %v", err)
	}
	st := u.Layout.ReadStatus()
	if st == nil || st.Version != "0.1.0" || st.AliveAt != "2026-10-07T20:00:00Z" || len(st.Keys) != 1 {
		t.Fatalf("status = %+v", st)
	}
}

func TestRunEndsWithTheContext(t *testing.T) {
	k := newKeys(t)
	u, _ := newUpdater(t, k)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if err := u.Run(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Run = %v", err)
	}
}

func TestReadRegularLimit(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "f")
	if err := os.WriteFile(p, make([]byte, 100), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := readRegular(p, 99); err == nil {
		t.Fatal("oversized file read")
	}
	if b, err := readRegular(p, 100); err != nil || len(b) != 100 {
		t.Fatalf("%d, %v", len(b), err)
	}
	if _, err := readRegular(dir, 100); err == nil {
		t.Fatal("directory read as a file")
	}
}

func TestWriteAtomicDoesNotFollowAPlantedLink(t *testing.T) {
	dir := t.TempDir()
	victim := filepath.Join(dir, "victim")
	if err := os.WriteFile(victim, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(dir, "status.json")
	if err := os.Symlink(victim, target+".tmp"); err != nil {
		t.Skip("no symlinks here:", err)
	}
	if err := writeAtomic(target, []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(victim); string(b) != "keep" {
		t.Fatalf("the link was followed: victim = %q", b)
	}
	if b, _ := os.ReadFile(target); string(b) != "{}" {
		t.Fatalf("target = %q", b)
	}
}

func TestUpdaterStateStaysInItsOwnDirectory(t *testing.T) {
	k := newKeys(t)
	node, state := t.TempDir(), t.TempDir()
	u, _ := newUpdater(t, k)
	u.Layout = NewLayout(node).WithState(state)
	if err := u.Layout.EnsureDir(); err != nil {
		t.Fatal(err)
	}
	h := u.Host.(*fakeHost)
	h.layout = u.Layout
	h.onStart = func(program []byte) {
		if v, ok := strings.CutPrefix(string(program), "NODE "); ok {
			_ = u.Layout.WriteConfirmed(v, time.Now())
		}
	}
	stage(t, u, k, nil, nil)
	if out, err := u.Apply(context.Background()); out != Applied || err != nil {
		t.Fatalf("out=%v err=%v", out, err)
	}
	u.writeStatus()
	for _, name := range []string{resultName, statusName} {
		if _, err := os.Stat(filepath.Join(state, name)); err != nil {
			t.Errorf("%s is not in the state directory: %v", name, err)
		}
		if _, err := os.Stat(filepath.Join(u.Layout.Dir, name)); err == nil {
			t.Errorf("%s was written into the node's directory", name)
		}
	}
	if u.Layout.ReadStatus() == nil {
		t.Fatal("the node cannot read the heartbeat")
	}
}

func TestTakeResultReportsEachResultOnce(t *testing.T) {
	l := NewLayout(t.TempDir()).WithState(t.TempDir())
	if err := l.EnsureDir(); err != nil {
		t.Fatal(err)
	}
	if l.TakeResult() != nil {
		t.Fatal("a result from nowhere")
	}
	put := func(id string) {
		if err := writeJSON(l.result(), Result{ID: id, OK: true, Version: "0.2.0", At: "2026-10-07T20:00:00Z"}); err != nil {
			t.Fatal(err)
		}
	}
	put("a")
	if r := l.TakeResult(); r == nil || r.Version != "0.2.0" {
		t.Fatalf("first take = %+v", r)
	}
	if l.TakeResult() != nil {
		t.Fatal("the same result twice")
	}
	put("b")
	if l.TakeResult() == nil {
		t.Fatal("a new result with the same content was swallowed")
	}
}

func TestTheUpdaterRefusesDirectoriesThatAreLinks(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("creating symlinks needs privileges on Windows")
	}
	k := newKeys(t)
	data := t.TempDir()
	elsewhere := t.TempDir()
	if err := os.Symlink(elsewhere, filepath.Join(data, dirName)); err != nil {
		t.Fatal(err)
	}
	u, h := newUpdater(t, k)
	u.Layout = NewLayout(data)
	if err := u.Layout.EnsureDir(); err == nil {
		t.Fatal("EnsureDir accepted a linked update directory")
	}
	if out, err := u.Apply(context.Background()); out != NothingToDo || err == nil || len(h.log) != 0 {
		t.Fatalf("Apply: out=%v err=%v log=%v", out, err, h.log)
	}
	entries, _ := os.ReadDir(elsewhere)
	if len(entries) != 0 {
		t.Fatalf("something was written through the link: %v", entries)
	}
}
