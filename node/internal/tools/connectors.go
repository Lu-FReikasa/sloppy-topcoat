package tools

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// Connectors (E2-04c): read-only capabilities that ask another system on the network — Uptime
// Kuma, the Omada controller — on behalf of the assistant. The credentials for those systems live
// in ONE file on the tool node, created by the maintainer with mode 600: never in the hub's
// configuration, never in an output, never in the audit log. The file is read here and nowhere
// else; a capability's input never holds a URL or a credential, so a model cannot point a
// connector somewhere else or ask it for a secret.
//
//	{
//	  "uptime_kuma": {"url": "http://10.0.0.x:3001", "api_key": "uk1_…"},   // api_key optional
//	  "omada": {"url": "https://10.0.0.x:8043", "omada_id": "…", "client_id": "…",
//	            "client_secret": "…", "site": "Default"},
//	  "keycloak": {"url": "https://10.0.0.x:8443", "realm": "home", "client_id": "…",
//	               "client_secret": "…"}
//	}
//
// Either section may be missing; a missing one switches its capabilities off (they are not offered
// to the hub at all).

// KumaConfig is the "uptime_kuma" section. APIKey is optional: an Uptime Kuma without its own
// login ("API keys are disabled because authentication is disabled") serves /metrics openly, and
// a reverse proxy lets only the tool node reach it; an empty key sends no Authorization header.
type KumaConfig struct {
	URL    string `json:"url"`
	APIKey string `json:"api_key"`
	TLS
}

// OmadaConfig is the "omada" section: an Open API application with client credentials (role Viewer).
type OmadaConfig struct {
	URL          string `json:"url"`
	OmadaID      string `json:"omada_id"`
	ClientID     string `json:"client_id"`
	ClientSecret string `json:"client_secret"`
	// Site is the name (or id) of the site to look at when a call names none; optional when the
	// controller has exactly one site.
	Site string `json:"site"`
	TLS
}

// KeycloakConfig is the "keycloak" section: a confidential client with a service account that has
// only read roles of the realm-management client (view-users, query-users, query-groups,
// view-clients, query-clients). It is the source of the identity view (E3-12).
type KeycloakConfig struct {
	URL          string `json:"url"`
	Realm        string `json:"realm"`
	ClientID     string `json:"client_id"`
	ClientSecret string `json:"client_secret"`
	TLS
}

// TLS is how a connector checks the certificate of the other system. The default is the system's
// trust store; a controller with a self-made certificate needs its CA file (preferred) or, as the
// maintainer's explicit decision, insecure_skip_verify.
type TLS struct {
	CAFile   string `json:"ca_file"`
	Insecure bool   `json:"insecure_skip_verify"`
	// Transport replaces the network (never set from the file): the fake tool node answers from
	// memory, so the integration tests need neither a listening port nor a real system.
	Transport http.RoundTripper `json:"-"`
}

// Connectors is the parsed file.
type Connectors struct {
	Kuma     *KumaConfig     `json:"uptime_kuma"`
	Omada    *OmadaConfig    `json:"omada"`
	Keycloak *KeycloakConfig `json:"keycloak"`
}

// DefaultConnectorsFile is where the node looks for the file when config.tools names none.
func DefaultConnectorsFile() string {
	if runtime.GOOS == "windows" {
		base := os.Getenv("ProgramData")
		if base == "" {
			base = `C:\ProgramData`
		}
		return filepath.Join(base, "topcoat-node", "connectors.json")
	}
	return "/etc/topcoat-node/connectors.json"
}

// ErrNoConnectorsFile says there is no file: no connector is configured, which is fine.
var ErrNoConnectorsFile = errors.New("no connectors file")

// LoadConnectors reads and checks the file. A missing file is ErrNoConnectorsFile. A file that
// others can read (mode other than 600), that is a link, or that belongs to somebody else is
// refused: a secret in a world-readable file is not a secret.
func LoadConnectors(path string) (*Connectors, error) {
	if path == "" {
		path = DefaultConnectorsFile()
	}
	st, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrNoConnectorsFile
	}
	if err != nil {
		return nil, fmt.Errorf("connectors file: %w", err)
	}
	if err := checkSecretFile(path, st); err != nil {
		return nil, err
	}
	b, err := os.ReadFile(path) //nolint:gosec // the maintainer's own file, checked above
	if err != nil {
		return nil, fmt.Errorf("connectors file: %w", err)
	}
	var c Connectors
	dec := json.NewDecoder(strings.NewReader(string(b)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		// the message may quote a value: name only the place
		return nil, errors.New("connectors file: not valid JSON of the documented shape (see INSTALL-NODE)")
	}
	if c.Kuma != nil {
		if err := checkURL("uptime_kuma", c.Kuma.URL); err != nil {
			return nil, err
		}
	}
	if c.Omada != nil {
		if err := checkURL("omada", c.Omada.URL); err != nil {
			return nil, err
		}
		if c.Omada.OmadaID == "" || c.Omada.ClientID == "" || c.Omada.ClientSecret == "" {
			return nil, errors.New("connectors file: omada needs omada_id, client_id and client_secret")
		}
	}
	if c.Keycloak != nil {
		if err := checkURL("keycloak", c.Keycloak.URL); err != nil {
			return nil, err
		}
		if c.Keycloak.Realm == "" || c.Keycloak.ClientID == "" || c.Keycloak.ClientSecret == "" {
			return nil, errors.New("connectors file: keycloak needs realm, client_id and client_secret")
		}
	}
	return &c, nil
}

func checkURL(section, raw string) error {
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return fmt.Errorf("connectors file: %s.url must be an http(s) URL", section)
	}
	if u.User != nil {
		return fmt.Errorf("connectors file: %s.url must not hold a user name or password", section)
	}
	return nil
}

// secrets are the values that must never appear in an output.
func (c *Connectors) secrets() []string {
	var out []string
	if c == nil {
		return out
	}
	if c.Kuma != nil && c.Kuma.APIKey != "" {
		out = append(out, c.Kuma.APIKey)
	}
	if c.Omada != nil {
		out = append(out, c.Omada.ClientSecret, c.Omada.ClientID)
	}
	if c.Keycloak != nil {
		out = append(out, c.Keycloak.ClientSecret, c.Keycloak.ClientID)
	}
	return out
}

// --- HTTP ---------------------------------------------------------------------------------

const (
	connectorTimeout   = 15 * time.Second
	maxConnectorResult = 4 << 20 // bytes read from the other system
)

func (t TLS) client() (*http.Client, error) {
	cfg := &tls.Config{MinVersion: tls.VersionTLS12}
	if t.CAFile != "" {
		pem, err := os.ReadFile(t.CAFile)
		if err != nil {
			return nil, errors.New("connectors file: the ca_file cannot be read")
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return nil, errors.New("connectors file: the ca_file holds no certificate")
		}
		cfg.RootCAs = pool
	}
	if t.Insecure {
		cfg.InsecureSkipVerify = true //nolint:gosec // the maintainer's explicit choice for a self-signed controller on the LAN
	}
	var rt http.RoundTripper = &http.Transport{TLSClientConfig: cfg, Proxy: nil, DisableKeepAlives: true}
	if t.Transport != nil {
		rt = t.Transport
	}
	return &http.Client{
		Timeout:   connectorTimeout,
		Transport: rt,
		// A redirect would carry the credentials to another place: never followed.
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}, nil
}

// fetch runs a request and returns the body (at most maxConnectorResult bytes) and the status.
// Errors never contain the request: a header holds a secret.
func fetch(client *http.Client, req *http.Request, name string) ([]byte, int, error) {
	resp, err := client.Do(req)
	if err != nil {
		var ue *url.Error
		if errors.As(err, &ue) {
			err = ue.Err // without the URL
		}
		return nil, 0, fmt.Errorf("%s is not reachable: %w", name, err)
	}
	defer resp.Body.Close() //nolint:errcheck // read-only
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxConnectorResult+1))
	if err != nil {
		return nil, resp.StatusCode, fmt.Errorf("%s: reading the answer: %w", name, err)
	}
	if len(body) > maxConnectorResult {
		return nil, resp.StatusCode, fmt.Errorf("%s answered more than %d MiB", name, maxConnectorResult>>20)
	}
	if resp.StatusCode >= 300 && resp.StatusCode < 400 {
		return nil, resp.StatusCode, fmt.Errorf("%s answered with a redirect (HTTP %d): check the url in the connectors file", name, resp.StatusCode)
	}
	return body, resp.StatusCode, nil
}
