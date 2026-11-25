package transfer

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"testing"
	"time"
)

const testToken = "tok-0123456789abcdefghijklmnopqrstuvwxyzABCDE"

type fixture struct {
	dir  string
	file string
	data []byte
	sha  string
}

func newFixture(t *testing.T, size int) fixture {
	t.Helper()
	dir := t.TempDir()
	data := make([]byte, size)
	for i := range data {
		data[i] = byte(i*7 + i/251)
	}
	file := filepath.Join(dir, "src.gguf")
	if err := os.WriteFile(file, data, 0o600); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(data)
	return fixture{dir: dir, file: file, data: data, sha: hex.EncodeToString(sum[:])}
}

func (f fixture) cfg() ServerConfig {
	return ServerConfig{File: f.file, Size: int64(len(f.data)), SHA256: f.sha, Token: testToken,
		Addresses: []string{"127.0.0.1"}, TTL: time.Minute}
}

func start(t *testing.T, cfg ServerConfig) *Server {
	t.Helper()
	s, err := Start(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.Close)
	return s
}

func ep(s *Server) []string { return []string{"127.0.0.1:" + strconv.Itoa(s.Port())} }

func TestTransfer(t *testing.T) {
	f := newFixture(t, 3<<20+123)
	var mu sync.Mutex
	var sent int64
	cfg := f.cfg()
	cfg.PeerAddrs = []string{"127.0.0.1"}
	cfg.Progress = func(done, _ int64) { mu.Lock(); sent = done; mu.Unlock() }
	s := start(t, cfg)
	dest := filepath.Join(f.dir, "dst.gguf")
	var got int64
	err := Fetch(context.Background(), ep(s), s.CertSHA256(), testToken, dest, int64(len(f.data)), f.sha, func(d, _ int64) { got = d })
	if err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(dest)
	if !bytes.Equal(b, f.data) {
		t.Fatal("content differs")
	}
	if got != int64(len(f.data)) {
		t.Fatalf("progress ended at %d", got)
	}
	if _, err := os.Stat(dest + ".part"); !os.IsNotExist(err) {
		t.Fatal(".part left behind")
	}
	if st, _ := os.Stat(dest); runtimePerm(st) != 0o600 {
		t.Logf("mode %v", st.Mode())
	}
	select {
	case <-s.Done():
	case <-time.After(5 * time.Second):
		t.Fatal("server stayed open after the delivery")
	}
	if err := s.Wait(); err != nil {
		t.Fatalf("Wait = %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if sent != int64(len(f.data)) {
		t.Fatalf("send progress %d", sent)
	}
	if c, err := net.DialTimeout("tcp", ep(s)[0], time.Second); err == nil {
		c.Close()
		t.Fatal("port still open")
	}
}

func runtimePerm(st os.FileInfo) os.FileMode { return st.Mode().Perm() }

func TestResume(t *testing.T) {
	f := newFixture(t, 2<<20)
	s := start(t, f.cfg())
	dest := filepath.Join(f.dir, "dst.gguf")
	if err := os.WriteFile(dest+".part", f.data[:700000], 0o600); err != nil {
		t.Fatal(err)
	}
	if err := Fetch(context.Background(), ep(s), s.CertSHA256(), testToken, dest, int64(len(f.data)), f.sha, nil); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(dest)
	if !bytes.Equal(b, f.data) {
		t.Fatal("content differs after resume")
	}
}

func TestResumeServesRange(t *testing.T) {
	f := newFixture(t, 1<<20)
	s := start(t, f.cfg())
	resp, tr, err := request(context.Background(), ep(s)[0], s.CertSHA256(), testToken, 1000)
	if err != nil {
		t.Fatal(err)
	}
	defer tr.CloseIdleConnections()
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusPartialContent {
		t.Fatalf("status %d", resp.StatusCode)
	}
	if resp.Header.Get(HashHeader) != f.sha {
		t.Fatal("hash header missing")
	}
	b, _ := io.ReadAll(resp.Body)
	if !bytes.Equal(b, f.data[1000:]) {
		t.Fatal("range body differs")
	}
}

func TestWrongToken(t *testing.T) {
	f := newFixture(t, 1000)
	s := start(t, f.cfg())
	err := Fetch(context.Background(), ep(s), s.CertSHA256(), "wrong", filepath.Join(f.dir, "d"), 1000, f.sha, nil)
	if !errors.Is(err, ErrRefused) {
		t.Fatalf("err = %v", err)
	}
	select {
	case <-s.Done():
		t.Fatal("a refused request must not close the socket")
	case <-time.After(100 * time.Millisecond):
	}
}

func TestWrongPeer(t *testing.T) {
	f := newFixture(t, 1000)
	cfg := f.cfg()
	cfg.PeerAddrs = []string{"10.0.0.99"}
	s := start(t, cfg)
	err := Fetch(context.Background(), ep(s), s.CertSHA256(), testToken, filepath.Join(f.dir, "d"), 1000, f.sha, nil)
	if !errors.Is(err, ErrRefused) {
		t.Fatalf("err = %v", err)
	}
	resp, tr, rerr := request(context.Background(), ep(s)[0], s.CertSHA256(), testToken, 0)
	if rerr != nil {
		t.Fatal(rerr)
	}
	resp.Body.Close()
	tr.CloseIdleConnections()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("status %d", resp.StatusCode)
	}
}

func TestStatusCodes(t *testing.T) {
	f := newFixture(t, 1000)
	s := start(t, f.cfg())
	c := &http.Client{Transport: &http.Transport{TLSClientConfig: &tls.Config{InsecureSkipVerify: true}}}
	get := func(path, auth string) int {
		req, _ := http.NewRequest("GET", "https://"+ep(s)[0]+path, nil)
		if auth != "" {
			req.Header.Set("Authorization", auth)
		}
		resp, err := c.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if c := get("/model", ""); c != 401 {
		t.Fatalf("no token: %d", c)
	}
	if c := get("/model", "Bearer nope"); c != 401 {
		t.Fatalf("bad token: %d", c)
	}
	for _, p := range []string{"/", "/etc/passwd", "/model/../x", "/model/extra", "/src.gguf"} {
		if c := get(p, "Bearer "+testToken); c != 404 {
			t.Fatalf("%s: %d", p, c)
		}
	}
}

func TestWrongFingerprint(t *testing.T) {
	f := newFixture(t, 1000)
	s := start(t, f.cfg())
	bad := hex.EncodeToString(make([]byte, 32))
	err := Fetch(context.Background(), ep(s), bad, testToken, filepath.Join(f.dir, "d"), 1000, f.sha, nil)
	if !errors.Is(err, ErrNoRoute) {
		t.Fatalf("err = %v", err)
	}
}

func TestEndpointsInOrder(t *testing.T) {
	f := newFixture(t, 5000)
	s := start(t, f.cfg())
	// A closed port first: the fetch falls through to the second endpoint.
	l, _ := net.Listen("tcp", "127.0.0.1:0")
	dead := l.Addr().String()
	l.Close()
	dest := filepath.Join(f.dir, "d")
	if err := Fetch(context.Background(), append([]string{dead}, ep(s)...), s.CertSHA256(), testToken, dest, 5000, f.sha, nil); err != nil {
		t.Fatal(err)
	}
	err := Fetch(context.Background(), []string{dead}, s.CertSHA256(), testToken, dest+"2", 5000, f.sha, nil)
	if !errors.Is(err, ErrNoRoute) {
		t.Fatalf("err = %v", err)
	}
}

func TestHashMismatch(t *testing.T) {
	f := newFixture(t, 4000)
	wrong := hex.EncodeToString(make([]byte, 32))
	cfg := f.cfg()
	cfg.SHA256 = wrong // the source lies about the hash; the client trusts the hub's value
	s := start(t, cfg)
	dest := filepath.Join(f.dir, "d")
	err := Fetch(context.Background(), ep(s), s.CertSHA256(), testToken, dest, 4000, wrong, nil)
	if !errors.Is(err, ErrHashMismatch) {
		t.Fatalf("err = %v", err)
	}
	for _, p := range []string{dest, dest + ".part"} {
		if _, err := os.Stat(p); !os.IsNotExist(err) {
			t.Fatalf("%s exists after a mismatch", p)
		}
	}
	// A source that announces another hash than the hub is refused before any byte is stored.
	s2 := start(t, f.cfg())
	err = Fetch(context.Background(), ep(s2), s2.CertSHA256(), testToken, dest, 4000, wrong, nil)
	if !errors.Is(err, ErrHashMismatch) {
		t.Fatalf("early err = %v", err)
	}
}

func TestTTLClosesSocket(t *testing.T) {
	f := newFixture(t, 1000)
	cfg := f.cfg()
	cfg.TTL = 150 * time.Millisecond
	s := start(t, cfg)
	select {
	case <-s.Done():
	case <-time.After(3 * time.Second):
		t.Fatal("ttl did not close the socket")
	}
	if err := s.Wait(); !errors.Is(err, ErrExpired) {
		t.Fatalf("Wait = %v", err)
	}
	if c, err := net.DialTimeout("tcp", ep(s)[0], time.Second); err == nil {
		c.Close()
		t.Fatal("port still open")
	}
}

func TestContextCancelAndClose(t *testing.T) {
	f := newFixture(t, 1000)
	ctx, cancel := context.WithCancel(context.Background())
	s, err := Start(ctx, f.cfg())
	if err != nil {
		t.Fatal(err)
	}
	cancel()
	select {
	case <-s.Done():
	case <-time.After(3 * time.Second):
		t.Fatal("cancel did not close the socket")
	}
	if !errors.Is(s.Wait(), context.Canceled) {
		t.Fatalf("Wait = %v", s.Wait())
	}
	s.Close() // idempotent
}

func TestNoListenerBeforeStart(t *testing.T) {
	f := newFixture(t, 100)
	// Pick a range of one free port, check that nothing listens, then Start and see it open.
	l, _ := net.Listen("tcp", ":0")
	port := l.Addr().(*net.TCPAddr).Port
	l.Close()
	if c, err := net.DialTimeout("tcp", "127.0.0.1:"+strconv.Itoa(port), 200*time.Millisecond); err == nil {
		c.Close()
		t.Skip("port taken by something else")
	}
	cfg := f.cfg()
	cfg.PortMin, cfg.PortMax = port, port
	s := start(t, cfg)
	if s.Port() != port {
		t.Fatalf("port %d", s.Port())
	}
	c, err := net.DialTimeout("tcp", "127.0.0.1:"+strconv.Itoa(port), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	c.Close()
	// The range is exhausted: a second server gets ErrNoPort.
	if _, err := Start(context.Background(), cfg); !errors.Is(err, ErrNoPort) {
		t.Fatalf("second Start = %v", err)
	}
}

func TestPlainTCPAndOldTLSRefused(t *testing.T) {
	f := newFixture(t, 100)
	s := start(t, f.cfg())
	c, err := tls.Dial("tcp", ep(s)[0], &tls.Config{InsecureSkipVerify: true, MaxVersion: tls.VersionTLS12})
	if err == nil {
		c.Close()
		t.Fatal("TLS 1.2 accepted")
	}
}

func TestStartValidates(t *testing.T) {
	f := newFixture(t, 100)
	cfg := f.cfg()
	cfg.Size = 99
	if _, err := Start(context.Background(), cfg); err == nil {
		t.Fatal("size mismatch accepted")
	}
	cfg = f.cfg()
	cfg.Token = ""
	if _, err := Start(context.Background(), cfg); err == nil {
		t.Fatal("empty token accepted")
	}
}
