// Package restartproxy is the write door to the Docker daemon: a tiny HTTP proxy that forwards
// exactly one request shape, POST /containers/<name>/restart[?t=N], and answers 403 to everything
// else (create, exec, stop, kill, start, images, GET, ...). It replaces the generic socket proxy
// for the write path: that image can only allow whole groups of endpoints (its restart switch also
// lets stop and kill through, found on real hardware). The check here is a
// whitelist on method, path and query, nothing is copied from the client request but that.
package restartproxy

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// restartPath is the one allowed path; an API version prefix (/v1.45/) is accepted because clients
// send it, the container reference follows the rules of the node's own capability input.
var restartPath = regexp.MustCompile(`^(?:/v[0-9]{1,2}\.[0-9]{1,3})?/containers/([A-Za-z0-9][A-Za-z0-9_.\-]{0,127})/restart$`)

// maxTimeout bounds the stop timeout a client may ask for (seconds).
const maxTimeout = 300

// Handler returns the proxy for an upstream Docker endpoint: a unix socket path (unix:///...) or
// a loopback address (tcp://127.0.0.1:2375, used by the tests).
func Handler(upstream string, logger *slog.Logger) (http.Handler, error) {
	u, err := url.Parse(upstream)
	if err != nil {
		return nil, fmt.Errorf("upstream %q: %w", upstream, err)
	}
	var tr *http.Transport
	base := ""
	switch u.Scheme {
	case "unix":
		if u.Path == "" {
			return nil, fmt.Errorf("upstream %q: no socket path", upstream)
		}
		tr = &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", u.Path)
		}}
		base = "http://docker"
	case "tcp":
		tr = &http.Transport{}
		base = "http://" + u.Host
	default:
		return nil, fmt.Errorf("upstream %q: use unix:///path or tcp://host:port", upstream)
	}
	client := &http.Client{Transport: tr, Timeout: 10 * time.Minute,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	if logger == nil {
		logger = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	return &proxy{client: client, base: base, log: logger}, nil
}

type proxy struct {
	client *http.Client
	base   string
	log    *slog.Logger
}

// check returns the container and the upstream query of an allowed request, or why it is not.
func check(r *http.Request) (container string, q url.Values, err error) {
	if r.Method != http.MethodPost {
		return "", nil, fmt.Errorf("method %s is not allowed", r.Method)
	}
	// the raw path must be the plain one: no escapes, no dot segments, no double slashes
	if r.URL.RawPath != "" || strings.Contains(r.URL.Path, "%") {
		return "", nil, errors.New("escaped paths are not allowed")
	}
	m := restartPath.FindStringSubmatch(r.URL.Path)
	if m == nil {
		return "", nil, errors.New("only POST /containers/<name>/restart is allowed")
	}
	if r.ContentLength > 0 {
		return "", nil, errors.New("a restart carries no body")
	}
	vals, perr := url.ParseQuery(r.URL.RawQuery)
	if perr != nil {
		return "", nil, errors.New("bad query")
	}
	q = url.Values{}
	for k, v := range vals {
		if k != "t" || len(v) != 1 {
			return "", nil, fmt.Errorf("query parameter %q is not allowed", k)
		}
		n, err := strconv.Atoi(v[0])
		if err != nil || n < 0 || n > maxTimeout {
			return "", nil, fmt.Errorf("t must be 0..%d", maxTimeout)
		}
		q.Set("t", strconv.Itoa(n))
	}
	return m[1], q, nil
}

func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	container, q, err := check(r)
	if err != nil {
		p.log.Warn("refused", "method", r.Method, "path", r.URL.Path, "reason", err.Error())
		http.Error(w, "forbidden: "+err.Error(), http.StatusForbidden)
		return
	}
	// a fresh request: nothing of the client's headers or body is passed on; the unversioned path
	// is used so the daemon's default API version applies
	target := p.base + "/containers/" + container + "/restart"
	if len(q) > 0 {
		target += "?" + q.Encode()
	}
	// target is the fixed upstream plus a container name and a number that check() vetted
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, target, nil) //nolint:gosec // see above
	if err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	resp, err := p.client.Do(req) //nolint:gosec // the request is the one built above
	if err != nil {
		p.log.Error("docker unreachable", "err", err)
		http.Error(w, "the Docker daemon is not reachable", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close() //nolint:errcheck // only the status and a short message are passed back
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<10))
	p.log.Info("restart forwarded", "container", container, "status", resp.StatusCode)
	w.WriteHeader(resp.StatusCode)
	_, _ = w.Write(body)
}

// Serve listens on addr, which must be a loopback address, and serves until ctx ends.
func Serve(ctx context.Context, addr, upstream string, logger *slog.Logger) error {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return fmt.Errorf("listen address %q: %w", addr, err)
	}
	if ip := net.ParseIP(host); host != "localhost" && (ip == nil || !ip.IsLoopback()) {
		return fmt.Errorf("listen address %q: the restart proxy listens on the loopback only", addr)
	}
	h, err := Handler(upstream, logger)
	if err != nil {
		return err
	}
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return err
	}
	return serveOn(ctx, ln, h)
}

func serveOn(ctx context.Context, ln net.Listener, h http.Handler) error {
	srv := &http.Server{Handler: h, ReadHeaderTimeout: 10 * time.Second, MaxHeaderBytes: 16 << 10}
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(sctx)
	}()
	if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}
