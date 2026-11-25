// Package pairing implements `topcoat-node pair`: it generates the node key
// pair, registers the node with the hub using a one-time pairing code and
// stores the returned identity in the data directory.
package pairing

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"runtime"
	"strings"
	"time"

	"topcoat/node/internal/hubtls"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/store"
)

var (
	codeRe = regexp.MustCompile(`^[A-Z0-9]{4}-[A-Z0-9]{4}$`)
	nameRe = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
)

// Request is the POST /api/node/pair body (pair_request.schema.json).
type Request struct {
	Code         string `json:"code"`
	Name         string `json:"name"`
	OS           string `json:"os"`
	Arch         string `json:"arch"`
	PublicKey    string `json:"public_key"`
	AgentVersion string `json:"agent_version,omitempty"`
	// Role is gpu (default) or tools (E2-04: no models, read-only capabilities).
	Role string `json:"role,omitempty"`
}

// Response is the 200 response body (pair_response.schema.json). The node
// authenticates later with its key (D12), so there is no credential.
type Response struct {
	NodeID string `json:"node_id"`
}

// Options for Pair.
type Options struct {
	HubURL string
	Code   string
	Name   string // empty: derived from the hostname
	// Role is gpu (default, empty) or tools: what the hub configures this node for.
	Role         string
	AgentVersion string
	Dir          *store.Dir
	// CAFingerprint pins the hub CA ("sha256:<hex>", from the GUI's pair command).
	CAFingerprint string
	// CAFile is an admin-provided CA certificate (PEM) to trust instead.
	CAFile string
	// InsecureHTTP allows a plain-http hub URL (tests and local development only).
	InsecureHTTP bool
	// HTTPClient replaces the TLS setup above (tests only).
	HTTPClient *http.Client
	// Force overwrites an existing identity.
	Force bool
	// Hostname defaults to os.Hostname (injectable for tests).
	Hostname func() (string, error)
	GOOS     string // defaults to runtime.GOOS
	GOARCH   string // defaults to runtime.GOARCH
}

// HubError is returned when the hub rejects the pairing request.
type HubError struct {
	Status int
	Detail string
}

func (e *HubError) Error() string {
	switch e.Status {
	case http.StatusNotFound:
		return "pairing code not found: " + e.Detail
	case http.StatusGone:
		return "pairing code expired or already used: " + e.Detail
	case http.StatusBadRequest:
		return "hub rejected the pairing request: " + e.Detail
	}
	return fmt.Sprintf("hub returned HTTP %d: %s", e.Status, e.Detail)
}

// SanitizeName turns a hostname into a valid node name (^[a-z0-9][a-z0-9-]{0,62}$).
func SanitizeName(host string) string {
	host = strings.ToLower(strings.TrimSpace(host))
	// Only the first DNS label: "box.example.lan" -> "box".
	if i := strings.IndexByte(host, '.'); i >= 0 {
		host = host[:i]
	}
	var b strings.Builder
	lastDash := false
	for _, r := range host {
		ok := (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9')
		if ok {
			b.WriteRune(r)
			lastDash = false
			continue
		}
		if !lastDash && b.Len() > 0 {
			b.WriteByte('-')
			lastDash = true
		}
	}
	name := strings.Trim(b.String(), "-")
	if len(name) > 63 {
		name = strings.TrimRight(name[:63], "-")
	}
	if name == "" {
		name = "node"
	}
	return name
}

// ValidName reports whether name is a valid node name.
func ValidName(name string) bool { return nameRe.MatchString(name) }

// PairURL builds the pairing endpoint URL from the hub base URL.
func PairURL(hub string) (string, error) {
	u, err := url.Parse(strings.TrimSpace(hub))
	if err != nil {
		return "", fmt.Errorf("parse hub url: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", fmt.Errorf("hub url %q: scheme must be http or https", hub)
	}
	if u.Host == "" {
		return "", fmt.Errorf("hub url %q has no host", hub)
	}
	u.Path = strings.TrimRight(u.Path, "/") + "/api/node/pair"
	u.RawQuery, u.Fragment = "", ""
	return u.String(), nil
}

// Pair registers the node with the hub and stores node.json and node.key.
func Pair(ctx context.Context, o Options) (store.Identity, error) {
	var id store.Identity
	if o.Dir == nil {
		return id, errors.New("pair: data dir is required")
	}
	if o.Dir.HasIdentity() && !o.Force {
		return id, fmt.Errorf("node is already paired (%s exists); use --force to pair again", store.IdentityFile)
	}
	code := strings.ToUpper(strings.TrimSpace(o.Code))
	if !codeRe.MatchString(code) {
		return id, fmt.Errorf("pairing code %q must look like ABCD-1234", o.Code)
	}
	name := o.Name
	if name == "" {
		hn := o.Hostname
		if hn == nil {
			hn = os.Hostname
		}
		h, err := hn()
		if err != nil {
			return id, fmt.Errorf("hostname: %w (use --name)", err)
		}
		name = SanitizeName(h)
	}
	if !ValidName(name) {
		return id, fmt.Errorf("node name %q must match ^[a-z0-9][a-z0-9-]{0,62}$", name)
	}
	goos, goarch := o.GOOS, o.GOARCH
	if goos == "" {
		goos = runtime.GOOS
	}
	if goarch == "" {
		goarch = runtime.GOARCH
	}
	endpoint, err := PairURL(o.HubURL)
	if err != nil {
		return id, err
	}
	client, pin, caPEM, err := o.client()
	if err != nil {
		return id, err
	}
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return id, fmt.Errorf("generate key: %w", err)
	}
	req := Request{
		Code:         code,
		Name:         name,
		OS:           goos,
		Arch:         goarch,
		PublicKey:    base64.StdEncoding.EncodeToString(pub),
		AgentVersion: o.AgentVersion,
		Role:         o.Role,
	}
	body, err := json.Marshal(req)
	if err != nil {
		return id, err
	}
	hreq, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return id, err
	}
	hreq.Header.Set("Content-Type", "application/json")
	hreq.Header.Set("Accept", "application/json")
	resp, err := client.Do(hreq)
	if err != nil {
		return id, fmt.Errorf("contact hub: %w", hubtls.Explain(err))
	}
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return id, fmt.Errorf("read hub response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return id, &HubError{Status: resp.StatusCode, Detail: detail(raw)}
	}
	var pr Response
	if err := json.Unmarshal(raw, &pr); err != nil {
		return id, fmt.Errorf("parse hub response: %w", err)
	}
	if !protocol.IsUUID(pr.NodeID) {
		return id, errors.New("hub response is missing a valid node_id")
	}
	if pin != nil {
		if caPEM = pin.PEM(); caPEM == nil {
			return id, errors.New("pinned CA was not seen during the TLS handshake")
		}
	}
	// Key and CA first: node.json existing means "paired", so it is written last.
	if err := o.Dir.SavePrivateKey(priv); err != nil {
		return id, err
	}
	if caPEM != nil {
		err = o.Dir.SaveHubCA(caPEM)
	} else {
		err = o.Dir.RemoveHubCA()
	}
	if err != nil {
		return id, err
	}
	id = store.Identity{
		HubURL:       strings.TrimRight(strings.TrimSpace(o.HubURL), "/"),
		NodeID:       pr.NodeID,
		InsecureHTTP: o.InsecureHTTP,
	}
	if err := o.Dir.SaveIdentity(id); err != nil {
		return store.Identity{}, err
	}
	return id, nil
}

// client builds the HTTP client for pairing. It returns the pin to read the
// hub CA from after a pinned handshake, or the CA to store from --ca-file.
func (o Options) client() (*http.Client, *hubtls.Pin, []byte, error) {
	useTLS, err := hubtls.CheckScheme(o.HubURL, o.InsecureHTTP)
	if err != nil {
		return nil, nil, nil, err
	}
	if o.CAFingerprint != "" && o.CAFile != "" {
		return nil, nil, nil, errors.New("use either --ca-fingerprint or --ca-file, not both")
	}
	if !useTLS && (o.CAFingerprint != "" || o.CAFile != "") {
		return nil, nil, nil, errors.New("--ca-fingerprint and --ca-file need an https:// hub URL")
	}
	if o.HTTPClient != nil {
		return o.HTTPClient, nil, nil, nil
	}
	const timeout = 30 * time.Second
	switch {
	case !useTLS:
		return hubtls.Client(nil, timeout), nil, nil, nil
	case o.CAFingerprint != "":
		fp, err := hubtls.ParseFingerprint(o.CAFingerprint)
		if err != nil {
			return nil, nil, nil, err
		}
		u, err := url.Parse(strings.TrimSpace(o.HubURL))
		if err != nil {
			return nil, nil, nil, err
		}
		cfg, pin := hubtls.PinnedConfig(fp, u.Hostname())
		return hubtls.Client(cfg, timeout), pin, nil, nil
	case o.CAFile != "":
		caPEM, err := os.ReadFile(o.CAFile)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("read --ca-file: %w", err)
		}
		cfg, err := hubtls.RootsConfig(caPEM)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("--ca-file %s: %w", o.CAFile, err)
		}
		return hubtls.Client(cfg, timeout), nil, caPEM, nil
	default:
		return hubtls.Client(hubtls.SystemConfig(), timeout), nil, nil, nil
	}
}

// detail extracts FastAPI's {"detail": ...} or falls back to the raw body.
func detail(raw []byte) string {
	var d struct {
		Detail json.RawMessage `json:"detail"`
	}
	if err := json.Unmarshal(raw, &d); err == nil && len(d.Detail) > 0 {
		var s string
		if json.Unmarshal(d.Detail, &s) == nil {
			return s
		}
		return string(d.Detail)
	}
	s := strings.TrimSpace(string(raw))
	if len(s) > 200 {
		s = s[:200] + "..."
	}
	if s == "" {
		s = "(no detail)"
	}
	return s
}
