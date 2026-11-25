package workshop

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"topcoat/node/internal/protocol"
)

// The workshop browser (E6-02): a headless Chromium in a throw-away container that the
// Development agent drives through named browser.* capabilities. It sees the built GUI (or the dev
// server) of the repository's worktree copy and a faked hub, never the network, and answers with
// data (accessibility tree, violations, sampled animation values) instead of pictures.
//
// The node talks to the helper inside the container through a mailbox directory, not a port: it
// writes in/<n>.json, the helper answers with out/<n>.json. The node opens no listening port and
// the container has no network at all (brief E6-02-workshop-browser).

const (
	defaultBrowserMemoryMB = 2048
	defaultBrowserCPUMilli = 2000
	defaultBrowserSeconds  = 1800 // hard upper limit of one session
	defaultBrowserIdle     = 300 * time.Second
	browserCallTimeout     = 90 * time.Second
	browserReadyTimeout    = 45 * time.Second
	// browserAnswerBytes bounds one answer; the node's own output limit is 64 KiB.
	browserAnswerBytes = 48 << 10
)

// BrowserSpec is what a session container is asked for.
type BrowserSpec struct {
	Image string
	// Dir is a copy of the worktree on the host, mounted read-only at /work; BoxDir is the mailbox,
	// mounted read-write at /box.
	Dir    string
	BoxDir string
	// NodeBoxDir is the same mailbox as the node itself sees it (differs from BoxDir when the node
	// runs in a container); empty = BoxDir.
	NodeBoxDir string
	// Mode is "built" (serve the built GUI) or "dev" (the repository's dev server); Path is the page
	// that is opened first.
	Mode     string
	Path     string
	MemoryMB int
	CPUMilli int
	// Timeout is the hard upper limit of the session.
	Timeout time.Duration
}

// BrowserSession is one open browser. Call sends one command to the helper and returns its answer.
type BrowserSession interface {
	Call(ctx context.Context, op string, args json.RawMessage) (json.RawMessage, error)
	Close(ctx context.Context) error
}

// BrowserRunner starts a browser session in a sealed container.
type BrowserRunner interface {
	Open(ctx context.Context, spec BrowserSpec) (BrowserSession, error)
}

// openBrowser is the one session of this node.
type openBrowser struct {
	id, repo string
	sess     BrowserSession
	runDir   string
	opened   time.Time
	last     time.Time
	cancel   context.CancelFunc
}

func (w *Workshop) browserIdle() time.Duration {
	if w.o.BrowserIdle > 0 {
		return w.o.BrowserIdle
	}
	return defaultBrowserIdle
}

// openSession starts the session for a repository. One session per node: a second open is refused
// until the first is closed (or ended by its idle limit).
// A theme (optional) goes into the mailbox as theme.json; the helper hands it to the faked hub.
func (w *Workshop) openSession(ctx context.Context, repo, mode, page string, theme []byte) (map[string]any, error) {
	rc := w.o.RunnerConfig
	image := rc.Images[protocol.WorkshopBrowser]
	if image == "" {
		return nil, inputErr("this node has no browser image (config.tools.workshop.runner.images.browser)")
	}
	dir, err := w.clone(repo)
	if err != nil {
		return nil, err
	}
	w.browserMu.Lock()
	defer w.browserMu.Unlock()
	if w.browser != nil {
		return nil, inputErr("a browser session is open (repository %s): close it first", w.browser.repo)
	}
	id := newRunID()
	runDir := filepath.Join(w.dir, ".runs", "browser-"+id)
	cleanup := func() { _ = os.RemoveAll(runDir) }
	work, box := filepath.Join(runDir, "work"), filepath.Join(runDir, "box")
	if err := w.copyWorktree(ctx, dir, work); err != nil {
		cleanup()
		return nil, err
	}
	for _, d := range []string{filepath.Join(box, "in"), filepath.Join(box, "out")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			cleanup()
			return nil, err
		}
	}
	// the helper runs unprivileged and, under a rootless Docker, as a uid of its own on the host: it
	// writes its answers into out/ and removes the commands it took from in/ (2026-11-15: with in/
	// at 0755 the first removal failed and the helper ended; every call after open timed out)
	for _, d := range []string{"in", "out"} {
		if err := os.Chmod(filepath.Join(box, d), 0o777); err != nil {
			cleanup()
			return nil, err
		}
	}
	if theme != nil {
		if err := os.WriteFile(filepath.Join(box, "theme.json"), theme, 0o644); err != nil {
			cleanup()
			return nil, err
		}
	}
	sctx, cancel := context.WithCancel(context.Background())
	sess, err := w.o.Browser.Open(ctx, BrowserSpec{
		Image: image, Dir: w.hostPath(work), BoxDir: w.hostPath(box), NodeBoxDir: box, Mode: mode, Path: page,
		MemoryMB: orDefault(rc.MemoryMB, defaultBrowserMemoryMB),
		CPUMilli: orDefault(rc.CPUMilli, defaultBrowserCPUMilli),
		Timeout:  time.Duration(defaultBrowserSeconds) * time.Second,
	})
	if err != nil {
		cancel()
		cleanup()
		return nil, fmt.Errorf("the browser could not start: %w", err)
	}
	now := w.o.Now()
	w.browser = &openBrowser{id: id, repo: repo, sess: sess, runDir: runDir, opened: now, last: now, cancel: cancel}
	go w.reapBrowser(sctx, id)
	out := map[string]any{"session": id, "repo": repo, "mode": mode, "path": page}
	if theme != nil {
		out["theme"] = true
	}
	return out, nil
}

// reapBrowser ends the session when it was not used for the idle limit or ran past the hard limit.
func (w *Workshop) reapBrowser(ctx context.Context, id string) {
	tick := w.browserIdle() / 4
	if tick < 20*time.Millisecond {
		tick = 20 * time.Millisecond
	}
	t := time.NewTicker(tick)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			w.browserMu.Lock()
			b := w.browser
			stale := b != nil && b.id == id && (w.o.Now().Sub(b.last) > w.browserIdle() ||
				w.o.Now().Sub(b.opened) > time.Duration(defaultBrowserSeconds)*time.Second)
			w.browserMu.Unlock()
			if stale {
				_ = w.closeSession(id)
				return
			}
		}
	}
}

// closeSession ends the session (id "" = whichever is open) and removes its files.
func (w *Workshop) closeSession(id string) error {
	w.browserMu.Lock()
	b := w.browser
	if b == nil || (id != "" && b.id != id) {
		w.browserMu.Unlock()
		return nil
	}
	w.browser = nil
	w.browserMu.Unlock()
	b.cancel()
	cctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	err := b.sess.Close(cctx)
	_ = os.RemoveAll(b.runDir)
	return err
}

// callSession sends one command to the open session. The answer is bounded; a longer one is an
// error that says to narrow the request, never a cut-off JSON text.
func (w *Workshop) callSession(ctx context.Context, op string, args json.RawMessage) (json.RawMessage, error) {
	w.browserMu.Lock()
	b := w.browser
	if b != nil {
		b.last = w.o.Now()
	}
	w.browserMu.Unlock()
	if b == nil {
		return nil, inputErr("no browser session is open: call browser.open first")
	}
	cctx, cancel := context.WithTimeout(ctx, browserCallTimeout)
	defer cancel()
	out, err := b.sess.Call(cctx, op, args)
	if err != nil {
		return nil, err
	}
	if len(out) > browserAnswerBytes {
		return nil, inputErr("the answer is larger than %d KiB: ask for less (a smaller scope, fewer nodes, fewer checks)", browserAnswerBytes>>10)
	}
	return out, nil
}

// --- the Docker session --------------------------------------------------------------------------

// browserBody is the container a session asks for (UNVERIFIED on a real Docker daemon: the tests
// use a fake API server). What it asks the daemon for is fixed here: no network at all, a read-only
// root file system, the worktree copy read-only, the mailbox as the only writable mount, all
// capabilities dropped, no new privileges, memory/CPU/process limits, a fixed command.
func browserBody(s BrowserSpec, uid, gid int) map[string]any {
	host := map[string]any{
		"Binds":          []string{s.Dir + ":/work:ro", s.BoxDir + ":/box:rw"},
		"NetworkMode":    "none",
		"Memory":         int64(s.MemoryMB) << 20,
		"MemorySwap":     int64(s.MemoryMB) << 20,
		"NanoCpus":       int64(s.CPUMilli) * 1_000_000,
		"PidsLimit":      1024,
		"ShmSize":        int64(512) << 20, // Chromium needs more than the default 64 MB
		"ReadonlyRootfs": true,
		"CapDrop":        []string{"ALL"},
		"SecurityOpt":    []string{"no-new-privileges"},
		"Privileged":     false,
		"Tmpfs":          map[string]string{"/tmp": "rw,exec,size=1g"},
	}
	return map[string]any{
		"Image": s.Image,
		"Cmd": []string{
			"node", "/opt/helper/helper.mjs", "--box", "/box", "--work", "/work",
			"--mode", s.Mode, "--path", s.Path, "--max-seconds", fmt.Sprint(int(s.Timeout.Seconds())),
		},
		"WorkingDir":      "/work",
		"User":            fmt.Sprintf("%d:%d", uid, gid),
		"Env":             []string{"HOME=/tmp", "CI=1"},
		"NetworkDisabled": true,
		"HostConfig":      host,
		"Labels":          map[string]string{"topcoat.workshop": "browser"},
	}
}

type dockerBrowser struct {
	r   *DockerRunner
	id  string
	box string // the mailbox on the node's own file system

	mu  sync.Mutex
	seq int
}

// Open starts the session container and waits until the helper says the browser is up
// (UNVERIFIED on real hardware).
func (r *DockerRunner) Open(ctx context.Context, s BrowserSpec) (BrowserSession, error) {
	uid, gid := os.Getuid(), os.Getgid()
	if uid == 0 {
		uid, gid = 65534, 65534
	}
	suffix := make([]byte, 6)
	_, _ = rand.Read(suffix)
	name := "topcoat-br-" + hex.EncodeToString(suffix)
	status, body, err := r.do(ctx, "POST", "/containers/create?name="+name, browserBody(s, uid, gid))
	if err != nil {
		return nil, err
	}
	if status != http.StatusCreated {
		if status == http.StatusNotFound {
			return nil, fmt.Errorf("image %q is not on this Docker host (pull it once by hand): %w", s.Image, dockerErr("create", status, body))
		}
		return nil, dockerErr("create", status, body)
	}
	var created struct {
		ID string `json:"Id"`
	}
	if err := json.Unmarshal(body, &created); err != nil || created.ID == "" {
		return nil, errors.New("docker create: no container id in the answer")
	}
	d := &dockerBrowser{r: r, id: created.ID, box: orString(s.NodeBoxDir, s.BoxDir)}
	if status, body, err = r.do(ctx, "POST", "/containers/"+d.id+"/start", nil); err != nil {
		d.remove()
		return nil, err
	} else if status != http.StatusNoContent && status != http.StatusNotModified {
		d.remove()
		return nil, dockerErr("start", status, body)
	}
	rctx, cancel := context.WithTimeout(ctx, browserReadyTimeout)
	defer cancel()
	raw, err := d.wait(rctx, "ready")
	if err != nil {
		d.remove()
		return nil, fmt.Errorf("the browser did not come up: %w", err)
	}
	var ready struct {
		Ready bool   `json:"ready"`
		Error string `json:"error"`
	}
	if err := json.Unmarshal(raw, &ready); err != nil || !ready.Ready {
		d.remove()
		return nil, fmt.Errorf("the browser did not come up: %s", strings.TrimSpace(ready.Error))
	}
	return d, nil
}

// wait polls for out/<name>.json and returns its content.
func (d *dockerBrowser) wait(ctx context.Context, name string) ([]byte, error) {
	file := filepath.Join(d.box, "out", name+".json")
	delay := 20 * time.Millisecond
	for {
		if b, err := os.ReadFile(file); err == nil {
			_ = os.Remove(file)
			return b, nil
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(delay):
		}
		if delay < 150*time.Millisecond {
			delay += 10 * time.Millisecond
		}
	}
}

func (d *dockerBrowser) Call(ctx context.Context, op string, args json.RawMessage) (json.RawMessage, error) {
	d.mu.Lock() // one command at a time
	defer d.mu.Unlock()
	d.seq++
	name := fmt.Sprintf("%06d", d.seq)
	if len(args) == 0 {
		args = json.RawMessage(`{}`)
	}
	msg, err := json.Marshal(map[string]any{"id": name, "op": op, "args": args})
	if err != nil {
		return nil, err
	}
	in := filepath.Join(d.box, "in")
	tmp := filepath.Join(in, name+".tmp")
	if err := os.WriteFile(tmp, msg, 0o644); err != nil {
		return nil, err
	}
	if err := os.Rename(tmp, filepath.Join(in, name+".json")); err != nil { // atomic: no half-written command
		return nil, err
	}
	raw, err := d.wait(ctx, name)
	if err != nil {
		return nil, fmt.Errorf("the browser did not answer (%s): %w", op, err)
	}
	var ans struct {
		OK     bool            `json:"ok"`
		Result json.RawMessage `json:"result"`
		Error  string          `json:"error"`
	}
	if err := json.Unmarshal(raw, &ans); err != nil {
		return nil, fmt.Errorf("the browser's answer is not JSON: %w", err)
	}
	if !ans.OK {
		return nil, inputErr("%s", strings.TrimSpace(ans.Error))
	}
	return ans.Result, nil
}

func (d *dockerBrowser) Close(ctx context.Context) error {
	// ask politely, then make sure: the container is removed whatever happened
	_ = os.WriteFile(filepath.Join(d.box, "in", "close.json"), []byte(`{"op":"close"}`), 0o644)
	_, _, _ = d.r.do(ctx, "POST", "/containers/"+d.id+"/kill", nil)
	d.remove()
	return nil
}

func (d *dockerBrowser) remove() {
	cctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	_, _, _ = d.r.do(cctx, "DELETE", "/containers/"+d.id+"?force=1&v=1", nil)
}

func orString(s, fallback string) string {
	if s != "" {
		return s
	}
	return fallback
}
