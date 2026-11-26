// Package transfer moves one model file directly from one node to another (E2-07, D51).
//
// The source opens a single-use TLS 1.3 socket only for the duration of one transfer
// ([Start]); the target pulls the file with [Fetch]. The socket serves exactly one thing,
// GET /model, and only to a peer address and a bearer token the hub chose. There is no CA:
// the source makes an ephemeral self-signed certificate and the target pins its SHA-256
// fingerprint, which the hub passes along over the authenticated sessions.
//
// UNVERIFIED on real hardware: everything here is tested on loopback only; routing between
// real nodes and the Windows firewall are not.
package transfer

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"net/netip"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Errors of a transfer.
var (
	// ErrNoRoute: no endpoint could be reached with a certificate that matches the pin.
	ErrNoRoute = errors.New("no route to the source node")
	// ErrHashMismatch: the received file does not have the announced SHA-256.
	ErrHashMismatch = errors.New("SHA-256 mismatch")
	// ErrRefused: the source answered with 401, 403 or 404.
	ErrRefused = errors.New("source refused the transfer")
	// ErrExpired: the source socket was closed because the TTL ran out.
	ErrExpired = errors.New("transfer socket expired")
	// ErrNoPort: no port of the configured range is free.
	ErrNoPort = errors.New("no free port in the transfer range")
)

// HashHeader carries the SHA-256 (hex) of the file.
const HashHeader = "X-Topcoat-Sha256"

// Path is the only URL path the socket serves.
const Path = "/model"

// DialTimeout is how long the target waits for one endpoint.
const DialTimeout = 5 * time.Second

// Progress is called with transferred and total bytes.
type Progress func(done, total int64)

// ServerConfig describes one single-use transfer socket.
type ServerConfig struct {
	// File is the absolute path of the model; Size and SHA256 (hex) describe it.
	File   string
	Size   int64
	SHA256 string
	// Token is the one-time bearer token.
	Token string
	// PeerAddrs are the IPs the single accepted peer may come from; empty = any.
	PeerAddrs []string
	// Addresses are put into the certificate (SAN).
	Addresses []string
	// PortMin..PortMax is the range tried in order; 0..0 lets the OS choose (tests).
	PortMin, PortMax int
	// TTL closes the socket when nothing has moved for this long (> 0).
	TTL time.Duration
	// Progress reports bytes served (absolute file offset); may be nil.
	Progress Progress
}

// Server is the single-use socket of the source.
type Server struct {
	cfg       ServerConfig
	srv       *http.Server
	ln        net.Listener
	port      int
	certFP    string
	peers     []netip.Addr
	activity  atomic.Int64 // unix nanos of the last sign of life
	delivered atomic.Bool

	closeOnce sync.Once
	done      chan struct{}
	mu        sync.Mutex
	err       error
}

// Start creates the certificate, binds the first free port and serves until the file was
// delivered to its end, the TTL ran out, ctx ended or Close is called.
func Start(ctx context.Context, cfg ServerConfig) (*Server, error) {
	if cfg.Token == "" || cfg.File == "" || cfg.TTL <= 0 {
		return nil, errors.New("transfer: file, token and ttl are required")
	}
	st, err := os.Stat(cfg.File)
	if err != nil {
		return nil, err
	}
	if st.Size() != cfg.Size {
		return nil, fmt.Errorf("transfer: %s has %d bytes, expected %d", cfg.File, st.Size(), cfg.Size)
	}
	s := &Server{cfg: cfg, done: make(chan struct{})}
	for _, p := range cfg.PeerAddrs {
		a, err := parseAddr(p)
		if err != nil {
			return nil, fmt.Errorf("transfer: peer address %q: %w", p, err)
		}
		s.peers = append(s.peers, a)
	}
	cert, fp, err := newCert(cfg.Addresses)
	if err != nil {
		return nil, err
	}
	s.certFP = fp
	ln, port, err := listenRange(cfg.PortMin, cfg.PortMax)
	if err != nil {
		return nil, err
	}
	s.ln, s.port = ln, port
	tlsLn := tls.NewListener(ln, &tls.Config{
		Certificates: []tls.Certificate{cert},
		MinVersion:   tls.VersionTLS13,
		NextProtos:   []string{"http/1.1"},
	})
	s.srv = &http.Server{
		Handler:           http.HandlerFunc(s.handle),
		ReadTimeout:       15 * time.Second, // also bounds the TLS handshake; the response is not limited
		ReadHeaderTimeout: 10 * time.Second,
		MaxHeaderBytes:    16 << 10,
		ErrorLog:          log.New(io.Discard, "", 0), // handshake noise of port scanners
	}
	s.touch()
	go func() { _ = s.srv.Serve(tlsLn) }()
	go s.watch(ctx)
	return s, nil
}

// Port is the bound port.
func (s *Server) Port() int { return s.port }

// CertSHA256 is the hex SHA-256 of the certificate the target must pin.
func (s *Server) CertSHA256() string { return s.certFP }

// Done is closed when the socket is closed.
func (s *Server) Done() <-chan struct{} { return s.done }

// Wait blocks until the socket is closed and returns nil if the file was delivered,
// else why it closed (ErrExpired, a context error or context.Canceled for Close).
func (s *Server) Wait() error {
	<-s.done
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.err
}

// Close closes the socket now (idempotent).
func (s *Server) Close() { s.finish(context.Canceled, false) }

func (s *Server) finish(cause error, graceful bool) {
	s.closeOnce.Do(func() {
		s.mu.Lock()
		s.err = cause
		s.mu.Unlock()
		go func() {
			defer close(s.done)
			if graceful {
				// Let the last response reach the target.
				c, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				if s.srv.Shutdown(c) == nil {
					return
				}
			}
			_ = s.srv.Close()
			_ = s.ln.Close()
		}()
	})
}

func (s *Server) touch() { s.activity.Store(time.Now().UnixNano()) }

// watch enforces the TTL (no sign of life for TTL) and the context.
func (s *Server) watch(ctx context.Context) {
	t := time.NewTimer(s.cfg.TTL)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			s.finish(ctx.Err(), false)
			return
		case <-s.done:
			return
		case <-t.C:
			idle := time.Since(time.Unix(0, s.activity.Load()))
			if idle >= s.cfg.TTL {
				s.finish(ErrExpired, false)
				return
			}
			t.Reset(s.cfg.TTL - idle)
		}
	}
}

func (s *Server) peerAllowed(remote string) bool {
	if len(s.peers) == 0 {
		return true
	}
	host, _, err := net.SplitHostPort(remote)
	if err != nil {
		return false
	}
	a, err := parseAddr(host)
	if err != nil {
		return false
	}
	for _, p := range s.peers {
		if p == a {
			return true
		}
	}
	return false
}

func (s *Server) tokenOK(r *http.Request) bool {
	h := r.Header.Get("Authorization")
	const prefix = "Bearer "
	if len(h) < len(prefix) || !strings.EqualFold(h[:len(prefix)], prefix) {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(h[len(prefix):]), []byte(s.cfg.Token)) == 1
}

func (s *Server) handle(w http.ResponseWriter, r *http.Request) {
	if !s.peerAllowed(r.RemoteAddr) {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	if !s.tokenOK(r) {
		w.Header().Set("WWW-Authenticate", "Bearer")
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	if r.Method != http.MethodGet || r.URL.Path != Path {
		http.NotFound(w, r)
		return
	}
	f, err := os.Open(s.cfg.File)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	defer f.Close()
	s.touch()
	cr := &countingReader{f: f, s: s}
	cw := &countingWriter{ResponseWriter: w, s: s}
	w.Header().Set(HashHeader, s.cfg.SHA256)
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Connection", "close")
	http.ServeContent(cw, r, "", time.Time{}, cr)
	if cw.err == nil && r.Context().Err() == nil && cr.end == s.cfg.Size && cw.status < 300 {
		if s.delivered.CompareAndSwap(false, true) {
			s.finish(nil, true)
		}
	}
}

// countingReader tracks the highest offset read and reports progress.
type countingReader struct {
	f   *os.File
	s   *Server
	pos int64
	end int64
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.f.Read(p)
	c.pos += int64(n)
	if c.pos > c.end {
		c.end = c.pos
	}
	if n > 0 {
		c.s.touch()
		if c.s.cfg.Progress != nil {
			c.s.cfg.Progress(c.pos, c.s.cfg.Size)
		}
	}
	return n, err
}

func (c *countingReader) Seek(off int64, whence int) (int64, error) {
	p, err := c.f.Seek(off, whence)
	if err == nil {
		c.pos = p
	}
	return p, err
}

// countingWriter remembers write errors and the status.
type countingWriter struct {
	http.ResponseWriter
	s      *Server
	err    error
	status int
}

func (c *countingWriter) WriteHeader(code int) {
	c.status = code
	c.ResponseWriter.WriteHeader(code)
}

func (c *countingWriter) Write(p []byte) (int, error) {
	if c.status == 0 {
		c.status = http.StatusOK
	}
	n, err := c.ResponseWriter.Write(p)
	if err != nil && c.err == nil {
		c.err = err
	}
	return n, err
}

func parseAddr(s string) (netip.Addr, error) {
	if i := strings.IndexByte(s, '%'); i >= 0 {
		s = s[:i]
	}
	a, err := netip.ParseAddr(strings.Trim(s, "[]"))
	if err != nil {
		return netip.Addr{}, err
	}
	return a.Unmap(), nil
}

// listenRange binds the first free port of min..max on all interfaces.
//
// All interfaces on purpose (gosec G102): this is the single-use transfer socket D51 allows. The
// target tries several of the source's addresses in turn (those the node reported, then the one the
// hub sees), so one bind address would not do. The socket accepts only the peer addresses the hub
// gave, a one-time token and pinned TLS 1.3, and closes after one file or its TTL.
func listenRange(min, max int) (net.Listener, int, error) {
	if min == 0 && max == 0 {
		ln, err := net.Listen("tcp", ":0") //nolint:gosec // G102: D51, see above
		if err != nil {
			return nil, 0, err
		}
		return ln, ln.Addr().(*net.TCPAddr).Port, nil
	}
	if min < 1 || max > 65535 || min > max {
		return nil, 0, fmt.Errorf("transfer: invalid port range %d-%d", min, max)
	}
	for p := min; p <= max; p++ {
		if ln, err := net.Listen("tcp", ":"+strconv.Itoa(p)); err == nil {
			return ln, p, nil
		}
	}
	return nil, 0, fmt.Errorf("%w (%d-%d)", ErrNoPort, min, max)
}

// newCert makes an ephemeral self-signed ECDSA P-256 certificate valid for about a day.
func newCert(addrs []string) (tls.Certificate, string, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 120))
	if err != nil {
		return tls.Certificate{}, "", err
	}
	now := time.Now()
	tmpl := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: "topcoat-transfer"},
		NotBefore:             now.Add(-5 * time.Minute),
		NotAfter:              now.Add(25 * time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
	}
	for _, a := range addrs {
		if ip, err := parseAddr(a); err == nil {
			tmpl.IPAddresses = append(tmpl.IPAddresses, net.IP(ip.AsSlice()))
		}
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	sum := sha256.Sum256(der)
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, hex.EncodeToString(sum[:]), nil
}

// --- client -----------------------------------------------------------------

// maxAttempts bounds the reconnects after a broken connection (each resumes the .part file).
const maxAttempts = 4

// Fetch pulls the file from the first endpoint ("host:port") that answers with a certificate
// whose SHA-256 is certSHA256, into dest (via dest+".part", resumed if present), verifies
// the SHA-256 and renames. The token is never logged or put into an error.
func Fetch(ctx context.Context, endpoints []string, certSHA256, token, dest string, size int64, sha string, progress Progress) error {
	if len(endpoints) == 0 {
		return fmt.Errorf("%w: no endpoints", ErrNoRoute)
	}
	certSHA256 = strings.ToLower(certSHA256)
	sha = strings.ToLower(sha)
	part := dest + ".part"
	h := sha256.New()
	var have int64
	if f, err := os.Open(part); err == nil {
		have, err = io.Copy(h, f)
		_ = f.Close()
		if err != nil {
			return err
		}
	}
	if have > size {
		_ = os.Remove(part)
		h.Reset()
		have = 0
	}
	var lastErr error
	for attempt := 0; have < size; attempt++ {
		if attempt >= maxAttempts {
			return fmt.Errorf("transfer broken %d times: %w", maxAttempts, lastErr)
		}
		err := fetchOnce(ctx, endpoints, certSHA256, token, part, size, sha, &have, h, progress)
		if err == nil {
			break
		}
		lastErr = err
		var te transientError
		if ctx.Err() != nil || !errors.As(err, &te) {
			return err
		}
	}
	if got := hex.EncodeToString(h.Sum(nil)); got != sha {
		_ = os.Remove(part)
		return fmt.Errorf("%w (got %s, expected %s); partial file removed", ErrHashMismatch, got, sha)
	}
	if err := os.Chmod(part, 0o600); err != nil {
		return err
	}
	if err := os.Rename(part, dest); err != nil {
		return err
	}
	if progress != nil {
		progress(size, size)
	}
	return nil
}

// transientError marks a failure after the connection was up; a retry resumes.
type transientError struct{ err error }

func (t transientError) Error() string { return t.err.Error() }
func (t transientError) Unwrap() error { return t.err }

func fetchOnce(ctx context.Context, endpoints []string, certSHA256, token, part string, size int64, sha string, have *int64, h hashWriter, progress Progress) error {
	var routeErrs []string
	for _, ep := range endpoints {
		resp, tr, err := request(ctx, ep, certSHA256, token, *have)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			routeErrs = append(routeErrs, fmt.Sprintf("%s: %v", ep, err))
			continue
		}
		err = receive(resp, part, size, sha, have, h, progress)
		_ = resp.Body.Close()
		tr.CloseIdleConnections()
		return err
	}
	return fmt.Errorf("%w (%s)", ErrNoRoute, strings.Join(routeErrs, "; "))
}

type hashWriter interface {
	io.Writer
	Reset()
}

// request connects to one endpoint, pins the certificate and sends the GET.
func request(ctx context.Context, endpoint, certSHA256, token string, from int64) (*http.Response, *http.Transport, error) {
	tr := &http.Transport{
		Proxy: nil,
		DialTLSContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			dctx, cancel := context.WithTimeout(ctx, DialTimeout)
			defer cancel()
			var d net.Dialer
			raw, err := d.DialContext(dctx, "tcp", addr)
			if err != nil {
				return nil, err
			}
			conn := tls.Client(raw, &tls.Config{
				MinVersion: tls.VersionTLS13,
				NextProtos: []string{"http/1.1"},
				// No CA: the certificate is self-signed and authenticated by its pin. The pin is
				// checked in VerifyConnection, which also runs for resumed sessions.
				InsecureSkipVerify: true, //nolint:gosec // pinned in VerifyConnection
				VerifyConnection: func(cs tls.ConnectionState) error {
					if len(cs.PeerCertificates) == 0 {
						return errors.New("no certificate")
					}
					sum := sha256.Sum256(cs.PeerCertificates[0].Raw)
					if subtle.ConstantTimeCompare([]byte(hex.EncodeToString(sum[:])), []byte(certSHA256)) != 1 {
						return errors.New("certificate does not match the pinned fingerprint")
					}
					return nil
				},
			})
			if err := conn.HandshakeContext(dctx); err != nil {
				_ = raw.Close()
				return nil, err
			}
			return conn, nil
		},
		TLSHandshakeTimeout:   DialTimeout,
		ResponseHeaderTimeout: 30 * time.Second,
		DisableCompression:    true,
		DisableKeepAlives:     true,
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://"+endpoint+Path, nil)
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("User-Agent", "topcoat-node")
	if from > 0 {
		req.Header.Set("Range", "bytes="+strconv.FormatInt(from, 10)+"-")
	}
	resp, err := (&http.Client{Transport: tr}).Do(req)
	if err != nil {
		// Do wraps with the URL, which contains no secret (the token is a header).
		return nil, nil, err
	}
	return resp, tr, nil
}

func receive(resp *http.Response, part string, size int64, sha string, have *int64, h hashWriter, progress Progress) error {
	flags := os.O_CREATE | os.O_WRONLY
	switch {
	case resp.StatusCode == http.StatusPartialContent && *have > 0:
		if !strings.HasPrefix(resp.Header.Get("Content-Range"), "bytes "+strconv.FormatInt(*have, 10)+"-") {
			return fmt.Errorf("source answered with a wrong range %q", resp.Header.Get("Content-Range"))
		}
		flags |= os.O_APPEND
	case resp.StatusCode == http.StatusOK:
		flags |= os.O_TRUNC
		h.Reset()
		*have = 0
	case resp.StatusCode == http.StatusUnauthorized, resp.StatusCode == http.StatusForbidden, resp.StatusCode == http.StatusNotFound:
		return fmt.Errorf("%w: HTTP %d", ErrRefused, resp.StatusCode)
	default:
		return fmt.Errorf("source answered HTTP %d", resp.StatusCode)
	}
	if got := strings.ToLower(resp.Header.Get(HashHeader)); got != sha {
		return fmt.Errorf("%w: the source announces SHA-256 %q, expected %s", ErrHashMismatch, got, sha)
	}
	if resp.ContentLength != size-*have {
		return fmt.Errorf("source announces %d bytes, expected %d", resp.ContentLength, size-*have)
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
			if *have+int64(n) > size {
				return errors.New("source sent more bytes than announced")
			}
			if _, err := f.Write(buf[:n]); err != nil {
				return err
			}
			_, _ = h.Write(buf[:n])
			*have += int64(n)
			if progress != nil {
				progress(*have, size)
			}
		}
		if errors.Is(rerr, io.EOF) {
			break
		}
		if rerr != nil {
			return transientError{fmt.Errorf("receive: %w (resumable)", rerr)}
		}
	}
	if *have != size {
		return transientError{fmt.Errorf("connection ended after %d of %d bytes (resumable)", *have, size)}
	}
	return f.Close()
}
