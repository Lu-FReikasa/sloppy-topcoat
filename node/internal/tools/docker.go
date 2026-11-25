package tools

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// Docker is the client of the Docker Engine API. It can do exactly one thing: GET a handful of
// read-only endpoints. There is no method for POST/PUT/DELETE in this type, and Get refuses every
// path outside dockerPaths, so a bug in a capability cannot reach "create container", "exec" or
// "restart". Together with a read-only socket proxy in front of the real socket (docs/INSTALL-NODE
// "Docker host"), the node has no way to change anything on the Docker host.
type Docker struct {
	client *http.Client
	base   string
}

// DefaultDockerHost is the socket of a local Docker daemon.
const DefaultDockerHost = "unix:///var/run/docker.sock"

// maxDockerBody caps what one API answer may be (a log of 500 very long lines).
const maxDockerBody = 4 << 20

// dockerPaths are the endpoints a capability may read.
var dockerPaths = []*regexp.Regexp{
	regexp.MustCompile(`^/containers/json$`),
	regexp.MustCompile(`^/containers/[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}/json$`),
	regexp.MustCompile(`^/containers/[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}/logs$`),
}

// NewDocker connects to host: "unix:///path/to.sock" or "tcp://127.0.0.1:2375" (a socket proxy
// on the loopback). Anything else, in particular a remote TCP address, is refused: the API has no
// authentication of its own and is not sent over a network.
func NewDocker(host string) (*Docker, error) {
	if host == "" {
		host = DefaultDockerHost
	}
	u, err := url.Parse(host)
	if err != nil {
		return nil, fmt.Errorf("docker_host %q: %w", host, err)
	}
	switch u.Scheme {
	case "unix":
		sock := u.Path
		if sock == "" {
			return nil, fmt.Errorf("docker_host %q: no socket path", host)
		}
		tr := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", sock)
		}, MaxIdleConns: 2, IdleConnTimeout: 30 * time.Second}
		return &Docker{client: &http.Client{Transport: tr}, base: "http://docker"}, nil
	case "tcp":
		h, _, err := net.SplitHostPort(u.Host)
		if err != nil {
			return nil, fmt.Errorf("docker_host %q: %w", host, err)
		}
		if ip := net.ParseIP(h); h != "localhost" && (ip == nil || !ip.IsLoopback()) {
			return nil, fmt.Errorf("docker_host %q: the Docker API is only reached over a unix socket or the loopback (put a read-only socket proxy on 127.0.0.1)", host)
		}
		return &Docker{client: &http.Client{Transport: &http.Transport{MaxIdleConns: 2, IdleConnTimeout: 30 * time.Second}}, base: "http://" + u.Host}, nil
	}
	return nil, fmt.Errorf("docker_host %q: use unix:///path or tcp://127.0.0.1:port", host)
}

// Get reads one allowed endpoint. It returns the body (at most maxDockerBody bytes) and whether
// the answer was longer.
func (d *Docker) Get(ctx context.Context, path string, q url.Values) ([]byte, bool, error) {
	ok := false
	for _, re := range dockerPaths {
		if re.MatchString(path) {
			ok = true
			break
		}
	}
	if !ok {
		return nil, false, fmt.Errorf("the Docker API path %q is not one this node reads", path)
	}
	u := d.base + path
	if len(q) > 0 {
		u += "?" + q.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, false, err
	}
	resp, err := d.client.Do(req)
	if err != nil {
		return nil, false, dockerUnreachable(err)
	}
	defer resp.Body.Close() //nolint:errcheck // read-only
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxDockerBody+1))
	if err != nil {
		return nil, false, err
	}
	over := len(body) > maxDockerBody
	if over {
		body = body[:maxDockerBody]
	}
	switch {
	case resp.StatusCode == http.StatusNotFound:
		return nil, false, inputErr("no such container")
	case resp.StatusCode == http.StatusForbidden:
		return nil, false, errors.New("the Docker API (or its socket proxy) refuses this request: " + firstLine(string(body)))
	case resp.StatusCode != http.StatusOK:
		return nil, false, fmt.Errorf("the Docker API answered %d: %s", resp.StatusCode, firstLine(string(body)))
	}
	return body, over, nil
}

func dockerUnreachable(err error) error {
	var op *net.OpError
	if errors.As(err, &op) {
		return fmt.Errorf("cannot reach the Docker API (is docker running, may the node's user read the socket?): %w", op.Err)
	}
	return fmt.Errorf("cannot reach the Docker API: %w", err)
}

func firstLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	if len(s) > 200 {
		s = s[:200]
	}
	return s
}

// demuxLogs turns the body of GET /containers/{id}/logs into text. A container without a TTY
// answers in frames of an 8-byte header (stream, 3 zero bytes, big-endian length) and payload;
// one with a TTY answers the raw stream.
func demuxLogs(body []byte, tty bool) string {
	if tty {
		return string(body)
	}
	var b strings.Builder
	for len(body) >= 8 {
		if body[1] != 0 || body[2] != 0 || body[3] != 0 || body[0] > 2 {
			// not a framed stream after all: take the rest as it is
			b.Write(body)
			return b.String()
		}
		n := int(binary.BigEndian.Uint32(body[4:8]))
		body = body[8:]
		if n > len(body) {
			n = len(body)
		}
		b.Write(body[:n])
		body = body[n:]
	}
	b.Write(body)
	return b.String()
}
