// Package hubtls builds the TLS configuration the node uses to reach the hub
// (ARCHITECTURE D11): a pinned hub CA learned at pairing time, an
// admin-provided CA file, or the system trust store. Plain HTTP is allowed
// only with an explicit opt-in (tests and local development).
package hubtls

import (
	"bytes"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"topcoat/node/internal/store"
)

// ErrInsecureHTTP is returned for http:// hub URLs without the opt-in.
var ErrInsecureHTTP = errors.New("hub URL uses plain http; use the https:// URL from the hub GUI " +
	"(or pass --insecure-http for tests and local development only)")

// ParseFingerprint parses "sha256:<64 hex digits>" (colons and case are ignored).
func ParseFingerprint(s string) ([]byte, error) {
	v := strings.ToLower(strings.TrimSpace(s))
	v = strings.TrimPrefix(v, "sha256:")
	v = strings.ReplaceAll(v, ":", "")
	b, err := hex.DecodeString(v)
	if err != nil || len(b) != sha256.Size {
		return nil, fmt.Errorf("CA fingerprint %q must look like sha256:<64 hex digits>", s)
	}
	return b, nil
}

// Fingerprint formats the SHA-256 of a DER certificate like the hub does.
func Fingerprint(der []byte) string {
	sum := sha256.Sum256(der)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// CheckScheme validates the hub URL scheme against the insecure-http opt-in
// and reports whether TLS is used.
func CheckScheme(hubURL string, insecureHTTP bool) (useTLS bool, err error) {
	u, err := url.Parse(strings.TrimSpace(hubURL))
	if err != nil {
		return false, fmt.Errorf("parse hub url: %w", err)
	}
	switch u.Scheme {
	case "https":
		return true, nil
	case "http":
		if !insecureHTTP {
			return false, ErrInsecureHTTP
		}
		return false, nil
	}
	return false, fmt.Errorf("hub url %q: scheme must be https", hubURL)
}

func baseConfig() *tls.Config {
	return &tls.Config{MinVersion: tls.VersionTLS12}
}

// SystemConfig trusts the operating system's CA store.
func SystemConfig() *tls.Config { return baseConfig() }

// RootsConfig trusts only the CA certificates in caPEM.
func RootsConfig(caPEM []byte) (*tls.Config, error) {
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return nil, errors.New("CA file contains no PEM certificate")
	}
	cfg := baseConfig()
	cfg.RootCAs = pool
	return cfg, nil
}

// Pin records the pinned certificate the hub presented (the CA to store).
type Pin struct {
	mu   sync.Mutex
	cert *x509.Certificate
}

// Certificate returns the matched certificate, or nil if no connection matched yet.
func (p *Pin) Certificate() *x509.Certificate {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.cert
}

// PEM returns the matched certificate as PEM, or nil.
func (p *Pin) PEM() []byte {
	c := p.Certificate()
	if c == nil {
		return nil
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: c.Raw})
}

// PinnedConfig accepts the hub only if a certificate in its chain has the
// SHA-256 fingerprint fp and the chain verifies against that certificate as
// the only root, with host (the host part of the hub URL) checked against the
// leaf. Used for pairing: afterwards the node trusts the stored CA through
// RootsConfig. The host is passed explicitly because the connection state
// does not carry it for IP addresses.
func PinnedConfig(fp []byte, host string) (*tls.Config, *Pin) {
	pin := &Pin{}
	cfg := baseConfig()
	// Standard verification is replaced by VerifyConnection below, which does
	// the full chain and host name check against the pinned certificate.
	cfg.InsecureSkipVerify = true //nolint:gosec // verified in VerifyConnection
	cfg.VerifyConnection = func(cs tls.ConnectionState) error {
		if len(cs.PeerCertificates) == 0 {
			return errors.New("hub presented no certificate")
		}
		if host == "" {
			return errors.New("no hub host name to verify")
		}
		for _, c := range cs.PeerCertificates {
			sum := sha256.Sum256(c.Raw)
			if !bytes.Equal(sum[:], fp) {
				continue
			}
			roots := x509.NewCertPool()
			roots.AddCert(c)
			inter := x509.NewCertPool()
			for _, o := range cs.PeerCertificates[1:] {
				inter.AddCert(o)
			}
			if _, err := cs.PeerCertificates[0].Verify(x509.VerifyOptions{
				DNSName:       host,
				Roots:         roots,
				Intermediates: inter,
			}); err != nil {
				return fmt.Errorf("hub certificate does not verify against the pinned CA: %w", err)
			}
			pin.mu.Lock()
			pin.cert = c
			pin.mu.Unlock()
			return nil
		}
		return fmt.Errorf("hub certificate chain does not contain the CA with fingerprint %s "+
			"(is the URL right, or did the hub get a new CA?)", "sha256:"+hex.EncodeToString(fp))
	}
	return cfg, pin
}

// Client returns an HTTP client using cfg (nil: plain HTTP only). It honors
// the proxy environment like http.DefaultTransport.
func Client(cfg *tls.Config, timeout time.Duration) *http.Client {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.TLSClientConfig = cfg
	return &http.Client{Transport: tr, Timeout: timeout}
}

// ForIdentity returns the TLS config for a paired node: the stored hub CA if
// there is one, else the system trust store; nil for an opted-in plain-http hub.
func ForIdentity(d *store.Dir, id store.Identity) (*tls.Config, error) {
	useTLS, err := CheckScheme(id.HubURL, id.InsecureHTTP)
	if err != nil || !useTLS {
		return nil, err
	}
	caPEM, ok, err := d.LoadHubCA()
	if err != nil {
		return nil, err
	}
	if !ok {
		return SystemConfig(), nil
	}
	cfg, err := RootsConfig(caPEM)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", store.HubCAFile, err)
	}
	return cfg, nil
}

// Explain adds a hint to certificate errors that are likely setup mistakes.
func Explain(err error) error {
	var host x509.HostnameError
	if errors.As(err, &host) {
		return fmt.Errorf("%w (add the name or IP you use in --hub to TOPCOAT_TLS_HOSTNAMES on the hub "+
			"and restart it)", err)
	}
	var unknown x509.UnknownAuthorityError
	if errors.As(err, &unknown) {
		return fmt.Errorf("%w (the hub certificate is not trusted: pair with the --ca-fingerprint "+
			"shown in the hub GUI, or --ca-file for your own CA)", err)
	}
	return err
}
