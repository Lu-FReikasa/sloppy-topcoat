package buildsvc

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

const (
	commit  = "0123456789abcdef0123456789abcdef01234567"
	buildID = "11111111-2222-4333-8444-555555555555"
)

// fakeRunner plays the fixed commands: it records every call and answers by its first words.
type fakeRunner struct {
	mu    sync.Mutex
	calls []string
	reply func(dir, line string) (string, error)
	block chan struct{}
}

func (f *fakeRunner) Run(_ context.Context, dir string, _ []string, name string, args ...string) (string, error) {
	line := name + " " + strings.Join(args, " ")
	f.mu.Lock()
	f.calls = append(f.calls, line)
	f.mu.Unlock()
	if f.block != nil {
		<-f.block
	}
	if f.reply != nil {
		return f.reply(dir, line)
	}
	return "", nil
}

func (f *fakeRunner) saw(prefix string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, c := range f.calls {
		if strings.HasPrefix(c, prefix) {
			n++
		}
	}
	return n
}

type fakeHost struct {
	uploads []Upload
	err     error
}

func (h *fakeHost) Upload(_ context.Context, u Upload) error {
	h.uploads = append(h.uploads, u)
	return h.err
}

func gitAnswers(dir, line string) (string, error) {
	switch {
	case strings.HasPrefix(line, "git rev-parse"):
		return commit + "\n", nil
	case strings.HasPrefix(line, "git log -1"):
		return "Add paging\n", nil
	case strings.HasPrefix(line, "git describe"):
		return "v0.2.0\n", nil
	case strings.HasPrefix(line, "git log --format"):
		return "a1b2c3 Add paging\nd4e5f6 Fix footer\n", nil
	case strings.HasPrefix(line, "git clone --mirror"):
		fields := strings.Fields(line)
		mirror := fields[len(fields)-1]
		_ = os.MkdirAll(mirror, 0o750)
		_ = os.WriteFile(filepath.Join(mirror, "HEAD"), []byte("ref: refs/heads/main"), 0o600)
		return "cloned", nil
	}
	return "", nil
}

func newSvc(t *testing.T, r *fakeRunner, h *fakeHost) *Service {
	t.Helper()
	s, err := New(Config{
		Work: t.TempDir(), Runner: r, Host: h, UpdateKeys: "PUBKEY",
		Sources: map[string]string{"release_repo": "/srv/release.git", "dev_mirror": "https://git.example.lan/dev.git"},
	})
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func call(t *testing.T, s *Service, action, input string) (map[string]any, error) {
	t.Helper()
	out, err := s.Handle(context.Background(), action, json.RawMessage(input))
	if err != nil {
		return nil, err
	}
	var m map[string]any
	if err := json.Unmarshal(out, &m); err != nil {
		t.Fatal(err)
	}
	return m, nil
}

func TestFetchNamesTheCommitAndTheChanges(t *testing.T) {
	r := &fakeRunner{reply: gitAnswers}
	s := newSvc(t, r, &fakeHost{})
	got, err := call(t, s, ActionFetch, `{"ref":"main","source":"release_repo"}`)
	if err != nil {
		t.Fatal(err)
	}
	if got["commit"] != commit || got["subject"] != "Add paging" {
		t.Fatalf("got %v", got)
	}
	if !strings.Contains(got["changes"].(string), "Fix footer") {
		t.Fatalf("changes: %v", got["changes"])
	}
	if r.saw("git clone --mirror -- /srv/release.git") != 1 {
		t.Fatalf("the first fetch clones the mirror: %v", r.calls)
	}
	// the second time the mirror is there and is updated, not cloned again
	if _, err := call(t, s, ActionFetch, `{"ref":"main","source":"release_repo"}`); err != nil {
		t.Fatal(err)
	}
	if r.saw("git clone --mirror") != 1 || r.saw("git remote update --prune") != 1 {
		t.Fatalf("calls: %v", r.calls)
	}
}

func TestEveryInputIsCheckedBeforeAnyCommandRuns(t *testing.T) {
	cases := []struct{ action, input string }{
		{ActionFetch, `{"ref":"main; rm -rf /","source":"release_repo"}`},
		{ActionFetch, `{"ref":"../etc","source":"release_repo"}`},
		{ActionFetch, `{"ref":"-x","source":"release_repo"}`},
		{ActionFetch, `{"ref":"main","source":"anywhere"}`},
		{ActionFetch, `{"ref":"main"}`},
		{ActionFetch, `{"ref":"main","source":"release_repo","cmd":"ls"}`},
		{ActionFetch, `[]`},
		{ActionNode, `{"commit":"main","version":"0.3.0","build_id":"` + buildID + `"}`},
		{ActionNode, `{"commit":"` + commit + `","version":"latest","build_id":"` + buildID + `"}`},
		{ActionNode, `{"commit":"` + commit + `","version":"0.3.0","build_id":"../x"}`},
		{ActionHubImage, `{"commit":"` + commit + `","build_id":"nope"}`},
		{ActionSmokeTest, `{"image":"--privileged"}`},
		{ActionSmokeTest, `{"image":"a b"}`},
		{"build.shell", `{}`},
	}
	for _, c := range cases {
		r := &fakeRunner{reply: gitAnswers}
		s := newSvc(t, r, &fakeHost{})
		if _, err := call(t, s, c.action, c.input); err == nil {
			t.Errorf("%s %s must be refused", c.action, c.input)
		}
		if len(r.calls) != 0 {
			t.Errorf("%s %s ran %v", c.action, c.input, r.calls)
		}
	}
}

func TestANodeReleaseIsBuiltFromAFreshCloneAtThePinnedCommitAndUploaded(t *testing.T) {
	var out string
	r := &fakeRunner{}
	r.reply = func(dir, line string) (string, error) {
		switch {
		case strings.HasPrefix(line, "bash ../scripts/build-node-release.sh"):
			out = strings.Fields(line)[len(strings.Fields(line))-1]
			_ = os.MkdirAll(out, 0o750)
			_ = os.WriteFile(filepath.Join(out, "topcoat-node-linux-amd64"), []byte("linux program"), 0o600)
			_ = os.WriteFile(filepath.Join(out, "topcoat-node-windows-amd64.exe"), []byte("windows program"), 0o600)
			_ = os.WriteFile(filepath.Join(out, "SHA256SUMS"), []byte("x"), 0o600)
			return "built ghp_" + strings.Repeat("a", 30), nil
		}
		return gitAnswers(dir, line)
	}
	h := &fakeHost{}
	s := newSvc(t, r, h)
	if _, err := call(t, s, ActionFetch, `{"ref":"main","source":"release_repo"}`); err != nil {
		t.Fatal(err)
	}
	got, err := call(t, s, ActionNode, `{"commit":"`+commit+`","version":"0.3.0","build_id":"`+buildID+`"}`)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte("linux program"))
	arts := got["artifacts"].([]any)
	if len(arts) != 2 {
		t.Fatalf("artifacts: %v", arts)
	}
	var linux map[string]any
	for _, a := range arts {
		if m := a.(map[string]any); m["os"] == "linux" {
			linux = m
		}
	}
	if linux["sha256"] != hex.EncodeToString(sum[:]) || linux["size"].(float64) != 13 {
		t.Fatalf("the checksum must be the node's own: %v", linux)
	}
	if len(h.uploads) != 2 || h.uploads[0].BuildID != buildID || !strings.HasPrefix(h.uploads[0].Path, s.cfg.Work) {
		t.Fatalf("uploads: %+v", h.uploads)
	}
	if r.saw("git clone --no-checkout") != 1 || r.saw("git -c advice.detachedHead=false checkout --detach "+commit) != 1 {
		t.Fatalf("a fresh clone at the pinned commit: %v", r.calls)
	}
	log := got["log"].(string)
	if strings.Contains(log, "ghp_") || !strings.Contains(log, "[masked]") {
		t.Fatalf("the log is cleaned: %q", log)
	}
}

func TestABuildNeedsAFetchedCommitAndStopsWhenAnUploadFails(t *testing.T) {
	r := &fakeRunner{reply: func(dir, line string) (string, error) {
		if strings.HasPrefix(line, "git cat-file") {
			return "", errors.New("missing")
		}
		return gitAnswers(dir, line)
	}}
	s := newSvc(t, r, &fakeHost{})
	if _, err := call(t, s, ActionNode, `{"commit":"`+commit+`","version":"0.3.0","build_id":"`+buildID+`"}`); err == nil || !strings.Contains(err.Error(), "build.fetch first") {
		t.Fatalf("got %v", err)
	}
	r2 := &fakeRunner{}
	r2.reply = func(dir, line string) (string, error) {
		if strings.HasPrefix(line, "bash ../scripts/build-node-release.sh") {
			out := strings.Fields(line)[len(strings.Fields(line))-1]
			_ = os.MkdirAll(out, 0o750)
			_ = os.WriteFile(filepath.Join(out, "topcoat-node-linux-amd64"), []byte("x"), 0o600)
		}
		return gitAnswers(dir, line)
	}
	h := &fakeHost{err: errors.New("hub said no")}
	s2 := newSvc(t, r2, h)
	if _, err := call(t, s2, ActionNode, `{"commit":"`+commit+`","version":"0.3.0","build_id":"`+buildID+`"}`); err == nil || !strings.Contains(err.Error(), "hub said no") {
		t.Fatalf("got %v", err)
	}
}

func TestAReleaseThatProducesNothingFails(t *testing.T) {
	r := &fakeRunner{reply: gitAnswers}
	s := newSvc(t, r, &fakeHost{})
	r.reply = func(dir, line string) (string, error) {
		if strings.HasPrefix(line, "bash ../scripts/build-node-release.sh") {
			_ = os.MkdirAll(strings.Fields(line)[len(strings.Fields(line))-1], 0o750)
		}
		return gitAnswers(dir, line)
	}
	if _, err := call(t, s, ActionNode, `{"commit":"`+commit+`","version":"0.3.0","build_id":"`+buildID+`"}`); err == nil || !strings.Contains(err.Error(), "no artifact") {
		t.Fatalf("got %v", err)
	}
}

func TestAHubImageIsBuiltInspectedAndSmokeTested(t *testing.T) {
	digest := "sha256:" + strings.Repeat("b", 64)
	r := &fakeRunner{}
	r.reply = func(dir, line string) (string, error) {
		switch {
		case strings.HasPrefix(line, "docker image inspect --format {{.Id}}"):
			return digest + "\n", nil
		case strings.HasPrefix(line, "docker image inspect --format {{.Size}}"):
			return "123456\n", nil
		case strings.HasPrefix(line, "bash scripts/smoke-test-hub-image.sh"):
			return "ok", nil
		}
		return gitAnswers(dir, line)
	}
	s := newSvc(t, r, &fakeHost{})
	if _, err := call(t, s, ActionSmokeTest, `{"image":"topcoat-hub:local-0123456"}`); err == nil {
		t.Fatal("the smoke test needs an image built here first")
	}
	got, err := call(t, s, ActionHubImage, `{"commit":"`+commit+`","build_id":"`+buildID+`"}`)
	if err != nil {
		t.Fatal(err)
	}
	if got["image"] != "topcoat-hub:local-0123456" || got["digest"] != digest || got["size"].(float64) != 123456 {
		t.Fatalf("got %v", got)
	}
	smoke, err := call(t, s, ActionSmokeTest, `{"image":"topcoat-hub:local-0123456"}`)
	if err != nil || smoke["ok"] != true {
		t.Fatalf("smoke: %v %v", smoke, err)
	}
	r.reply = func(dir, line string) (string, error) {
		if strings.HasPrefix(line, "bash scripts/smoke-test-hub-image.sh") {
			return "boom", errors.New("exit 1\nmore")
		}
		return gitAnswers(dir, line)
	}
	bad, err := call(t, s, ActionSmokeTest, `{"image":"topcoat-hub:local-0123456"}`)
	if err != nil || bad["ok"] != false || !strings.Contains(bad["summary"].(string), "exit 1") {
		t.Fatalf("a failed smoke test is a result, not an error: %v %v", bad, err)
	}
}

func TestADigestThatIsNotOneIsRefused(t *testing.T) {
	r := &fakeRunner{reply: func(dir, line string) (string, error) {
		if strings.HasPrefix(line, "docker image inspect") {
			return "not-a-digest", nil
		}
		return gitAnswers(dir, line)
	}}
	s := newSvc(t, r, &fakeHost{})
	if _, err := call(t, s, ActionHubImage, `{"commit":"`+commit+`","build_id":"`+buildID+`"}`); err == nil {
		t.Fatal("an image without a digest is not a result")
	}
}

func TestOneStepAtATime(t *testing.T) {
	block := make(chan struct{})
	r := &fakeRunner{reply: gitAnswers, block: block}
	s := newSvc(t, r, &fakeHost{})
	done := make(chan error, 1)
	go func() { _, err := call(t, s, ActionFetch, `{"ref":"main","source":"release_repo"}`); done <- err }()
	for r.saw("git clone") == 0 {
		// wait until the first call is inside the runner
		select {
		case err := <-done:
			t.Fatalf("returned early: %v", err)
		default:
		}
	}
	if _, err := call(t, s, ActionFetch, `{"ref":"main","source":"release_repo"}`); !errors.Is(err, ErrBusy) {
		t.Fatalf("got %v", err)
	}
	close(block)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestMaskCleansWhatLooksLikeASecret(t *testing.T) {
	in := "clone https://user:s3cretpass@git.example.lan/x.git\nAuthorization: Bearer abcdefghijklmnop1234\n" +
		"password=hunter22x\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n" +
		"topcoat-update-key-v1 " + strings.Repeat("A", 44) + "\nplain line"
	out := Mask(in)
	for _, secret := range []string{"s3cretpass", "abcdefghijklmnop1234", "hunter22x", "AAAA\n", "topcoat-update-key-v1 AAAA"} {
		if strings.Contains(out, secret) {
			t.Errorf("%q survived in %q", secret, out)
		}
	}
	if !strings.Contains(out, "plain line") || !strings.Contains(out, "Bearer [masked]") {
		t.Errorf("the rest stays: %q", out)
	}
}

func TestTheExecRunnerRunsAFixedCommandAndReportsAFailure(t *testing.T) {
	out, err := ExecRunner{}.Run(context.Background(), t.TempDir(), nil, "echo", "hello")
	if err != nil || strings.TrimSpace(out) != "hello" {
		t.Fatalf("out=%q err=%v", out, err)
	}
	if _, err := (ExecRunner{}).Run(context.Background(), t.TempDir(), nil, "false"); err == nil {
		t.Fatal("a failing command is an error")
	}
}
