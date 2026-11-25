package workshop

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// DockerRunner runs a test job in a throw-away container through the Docker Engine API
// (UNVERIFIED on a real Docker daemon: the tests use a fake API server).
//
// A run that is watched (Job.OnOutput or Job.IdleTimeout) has its log read every PollEvery
// (UNVERIFIED on real hardware: needs a log driver the logs endpoint can read, like json-file; with
// none the run is seen as "no news" and only the hard limit ends it).
//
// What it asks the daemon for is fixed here and not influenced by a repository: the image comes
// from the node's configuration, the command from the template table, the only mount is the copy
// of the worktree, the root file system is read-only, all capabilities are dropped, no new
// privileges, no network unless the template was released for one, memory/CPU/process limits, no
// environment but HOME and CI. The container is removed afterwards, whatever happened.
//
// Where it runs matters: give it a Docker (or Podman) daemon of its own, not the daemon that runs
// the family's services, because whoever may create containers on a daemon can in the end reach
// the machine (brief E6-01-dev-workshop, "Where the runner lives").
type DockerRunner struct {
	client *http.Client
	base   string
	// Now is the clock (tests).
	Now func() time.Time
	// PollEvery is how often the output of a run that is watched (Job.OnOutput, Job.IdleTimeout)
	// is looked at; 0 = 2 s.
	PollEvery time.Duration
}

const defaultPollEvery = 2 * time.Second

// NewDockerRunner connects to host: unix:///path or tcp://127.0.0.1:port (empty = the default socket).
func NewDockerRunner(host string) (*DockerRunner, error) {
	if host == "" {
		host = "unix:///var/run/docker.sock"
	}
	u, err := url.Parse(host)
	if err != nil {
		return nil, fmt.Errorf("docker_host %q: %w", host, err)
	}
	switch u.Scheme {
	case "unix":
		if u.Path == "" {
			return nil, fmt.Errorf("docker_host %q: no socket path", host)
		}
		sock := u.Path
		tr := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", sock)
		}}
		return &DockerRunner{client: &http.Client{Transport: tr}, base: "http://docker", Now: time.Now}, nil
	case "tcp":
		h, _, err := net.SplitHostPort(u.Host)
		if err != nil {
			return nil, fmt.Errorf("docker_host %q: %w", host, err)
		}
		if ip := net.ParseIP(h); h != "localhost" && (ip == nil || !ip.IsLoopback()) {
			return nil, fmt.Errorf("docker_host %q: the Docker API is only reached over a unix socket or the loopback", host)
		}
		return &DockerRunner{client: &http.Client{}, base: "http://" + u.Host, Now: time.Now}, nil
	}
	return nil, fmt.Errorf("docker_host %q: use unix:///path or tcp://127.0.0.1:port", host)
}

func (r *DockerRunner) do(ctx context.Context, method, path string, body any) (int, []byte, error) {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return 0, nil, err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, r.base+path, rd)
	if err != nil {
		return 0, nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := r.client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	return resp.StatusCode, data, err
}

func dockerErr(what string, status int, body []byte) error {
	var m struct {
		Message string `json:"message"`
	}
	_ = json.Unmarshal(body, &m)
	if m.Message == "" {
		m.Message = strings.TrimSpace(string(body))
	}
	if len(m.Message) > 300 {
		m.Message = m.Message[:300] + "…"
	}
	return fmt.Errorf("docker %s: %d %s", what, status, m.Message)
}

// createBody is the container the runner asks for.
func createBody(j Job, uid, gid int) map[string]any {
	host := map[string]any{
		"Binds":          []string{j.Dir + ":/work:rw"},
		"NetworkMode":    "none",
		"Memory":         int64(j.MemoryMB) << 20,
		"MemorySwap":     int64(j.MemoryMB) << 20,
		"NanoCpus":       int64(j.CPUMilli) * 1_000_000,
		"PidsLimit":      512,
		"ReadonlyRootfs": true,
		"CapDrop":        []string{"ALL"},
		"SecurityOpt":    []string{"no-new-privileges"},
		"Privileged":     false,
		"Tmpfs":          map[string]string{"/tmp": "rw,exec,size=1g"},
	}
	if j.Network != "" {
		host["NetworkMode"] = j.Network
	}
	return map[string]any{
		"Image":           j.Image,
		"Cmd":             j.Cmd,
		"WorkingDir":      "/work",
		"User":            fmt.Sprintf("%d:%d", uid, gid),
		"Env":             []string{"HOME=/tmp", "CI=1"},
		"NetworkDisabled": j.Network == "",
		"HostConfig":      host,
		"Labels":          map[string]string{"topcoat.workshop": "test-run"},
	}
}

// Run starts the job and waits for it.
func (r *DockerRunner) Run(ctx context.Context, j Job) (Result, error) {
	uid, gid := os.Getuid(), os.Getgid()
	if uid == 0 { // never run the test as root: make the copy usable for an unprivileged user
		uid, gid = 65534, 65534
		if err := openUp(j.Dir); err != nil {
			return Result{}, err
		}
	}
	suffix := make([]byte, 6)
	_, _ = rand.Read(suffix)
	name := "topcoat-ws-" + hex.EncodeToString(suffix)
	status, body, err := r.do(ctx, "POST", "/containers/create?name="+name, createBody(j, uid, gid))
	if err != nil {
		return Result{}, err
	}
	if status != http.StatusCreated {
		if status == http.StatusNotFound {
			return Result{}, fmt.Errorf("image %q is not on this Docker host (pull it once by hand): %w", j.Image, dockerErr("create", status, body))
		}
		return Result{}, dockerErr("create", status, body)
	}
	var created struct {
		ID string `json:"Id"`
	}
	if err := json.Unmarshal(body, &created); err != nil || created.ID == "" {
		return Result{}, errors.New("docker create: no container id in the answer")
	}
	id := created.ID
	// remove it however this ends, with a context that survives a canceled call
	defer func() {
		cctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		_, _, _ = r.do(cctx, "DELETE", "/containers/"+id+"?force=1&v=1", nil)
	}()

	started := r.Now()
	if status, body, err = r.do(ctx, "POST", "/containers/"+id+"/start", nil); err != nil {
		return Result{}, err
	} else if status != http.StatusNoContent && status != http.StatusNotModified {
		return Result{}, dockerErr("start", status, body)
	}
	type waited struct {
		status int
		body   []byte
		err    error
	}
	wctx, wcancel := context.WithCancel(ctx)
	defer wcancel()
	waitDone := make(chan waited, 1)
	go func() {
		st, b, e := r.do(wctx, "POST", "/containers/"+id+"/wait", nil)
		waitDone <- waited{st, b, e}
	}()
	hard := time.NewTimer(j.Timeout)
	defer hard.Stop()
	every := r.PollEvery
	if every <= 0 {
		every = defaultPollEvery
	}
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	watch := j.OnOutput != nil || j.IdleTimeout > 0
	var lastSum [32]byte
	lastChange := started
	kill := func() {
		kctx, kcancel := context.WithTimeout(context.Background(), 20*time.Second)
		_, _, _ = r.do(kctx, "POST", "/containers/"+id+"/kill", nil)
		kcancel()
	}
	res := Result{}
loop:
	for {
		select {
		case w := <-waitDone:
			switch {
			case w.err != nil:
				return Result{}, w.err
			case w.status != http.StatusOK:
				return Result{}, dockerErr("wait", w.status, w.body)
			}
			var body struct {
				StatusCode int `json:"StatusCode"`
			}
			_ = json.Unmarshal(w.body, &body)
			res.ExitCode = body.StatusCode
			break loop
		case <-hard.C:
			res.TimedOut = true
			res.ExitCode = 137
			kill()
			break loop
		case <-ctx.Done():
			kill()
			return Result{}, ctx.Err()
		case <-ticker.C:
			if !watch {
				continue
			}
			pctx, pcancel := context.WithTimeout(ctx, 10*time.Second)
			st, lb, lerr := r.do(pctx, "GET", "/containers/"+id+"/logs?stdout=1&stderr=1&tail=2000", nil)
			pcancel()
			if lerr != nil || st != http.StatusOK {
				continue // a look that failed is no sign of a stuck run
			}
			if sum := sha256.Sum256(lb); sum != lastSum {
				lastSum, lastChange = sum, r.Now()
				if j.OnOutput != nil {
					j.OnOutput(demux(lb))
				}
			}
			if j.IdleTimeout > 0 && r.Now().Sub(lastChange) > j.IdleTimeout {
				res.TimedOutIdle = true
				res.ExitCode = 137
				kill()
				break loop
			}
		}
	}
	res.Duration = r.Now().Sub(started)
	lctx, lcancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer lcancel()
	// the whole log, of which the start and the end are kept: a run's first part (one test file's
	// pytest result) was pushed out by a long second part (the image's web checks), 2026-11-19
	if status, body, err = r.do(lctx, "GET", "/containers/"+id+"/logs?stdout=1&stderr=1&tail=all", nil); err == nil && status == http.StatusOK {
		res.Output = keepEnds(demux(body), outputHeadBytes, outputTailBytes)
	}
	return res, nil
}

// The parts of a run's output that are kept: together below testOutputBytes, so the caller's own
// cut keeps both.
const (
	outputHeadBytes = 10 << 10
	outputTailBytes = 12 << 10
)

// keepEnds is s, or its first head and last tail bytes with a marker between them.
func keepEnds(s string, head, tail int) string {
	if len(s) <= head+tail {
		return s
	}
	return s[:head] + "\n[… the middle of the output left out]\n" + s[len(s)-tail:]
}

// demux reads Docker's multiplexed log stream: 8-byte header (stream, 0,0,0, big-endian size)
// followed by the payload.
func demux(b []byte) string {
	var out strings.Builder
	for len(b) >= 8 {
		n := int(binary.BigEndian.Uint32(b[4:8]))
		b = b[8:]
		if n > len(b) {
			n = len(b)
		}
		out.Write(b[:n])
		b = b[n:]
	}
	if out.Len() == 0 && len(b) > 0 { // a TTY-less log is always multiplexed; this is a fallback
		return string(b)
	}
	return out.String()
}

// openUp lets an unprivileged user write in the copy of the worktree.
func openUp(dir string) error {
	return filepath.WalkDir(dir, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		mode := os.FileMode(0o666)
		if d.IsDir() {
			mode = 0o777
		} else if info, err := d.Info(); err == nil && info.Mode()&0o111 != 0 {
			mode = 0o777
		}
		return os.Chmod(p, mode)
	})
}
