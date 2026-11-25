// Package sethub moves a paired node to a new hub address (`topcoat-node set-hub`).
//
// A hub that moved to another machine keeps its database and its CA (copy the hub's tls
// directory), so the node keeps its identity and its pinned CA and only needs the new URL. The
// new address is accepted only if the hub behind it presents a certificate that verifies against
// the CA this node pinned when it paired (or against the system store if it never pinned one),
// and answers like a topcoat hub. Anything else is refused, so a typo or a foreign server never
// ends up in node.json.
package sethub

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"topcoat/node/internal/hubtls"
	"topcoat/node/internal/store"
)

// Options for Move.
type Options struct {
	Dir    *store.Dir
	HubURL string
	// InsecureHTTP allows a plain-http hub URL (tests and local development only).
	InsecureHTTP bool
	// HTTPClient replaces the TLS setup (tests only).
	HTTPClient *http.Client
}

// Result describes what changed.
type Result struct {
	Old, New store.Identity
	// CA is the fingerprint of the pinned CA the new hub was verified against ("" = system store
	// or plain HTTP).
	CA string
}

// Move checks the hub at o.HubURL and, if it is a hub this node can trust, stores the address in
// node.json. The identity (node id, key) and the pinned CA stay as they are.
func Move(ctx context.Context, o Options) (Result, error) {
	var res Result
	old, err := o.Dir.LoadIdentity()
	if err != nil {
		return res, err
	}
	res.Old = old
	raw := strings.TrimRight(strings.TrimSpace(o.HubURL), "/")
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" || (u.Path != "" && u.Path != "/") {
		return res, fmt.Errorf("hub URL %q must look like https://host:port (no path)", o.HubURL)
	}
	useTLS, err := hubtls.CheckScheme(raw, o.InsecureHTTP)
	if err != nil {
		return res, err
	}
	next := store.Identity{HubURL: raw, NodeID: old.NodeID, InsecureHTTP: o.InsecureHTTP && !useTLS}

	client := o.HTTPClient
	if client == nil {
		cfg, err := hubtls.ForIdentity(o.Dir, next)
		if err != nil {
			return res, err
		}
		client = hubtls.Client(cfg, 15*time.Second)
	}
	if useTLS {
		if caPEM, ok, err := o.Dir.LoadHubCA(); err != nil {
			return res, err
		} else if ok {
			res.CA = pinnedFingerprint(caPEM)
		}
	}
	if err := checkHub(ctx, client, raw); err != nil {
		return res, err
	}
	if err := o.Dir.SaveIdentity(next); err != nil {
		return res, err
	}
	res.New = next
	return res, nil
}

// checkHub asks GET /api/health and expects the answer of a topcoat hub.
func checkHub(ctx context.Context, client *http.Client, base string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/api/health", nil)
	if err != nil {
		return err
	}
	resp, err := client.Do(req)
	if err != nil {
		return explain(base, err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	var h struct {
		Status  string `json:"status"`
		Version string `json:"version"`
	}
	if resp.StatusCode != http.StatusOK || json.Unmarshal(body, &h) != nil || h.Status != "ok" || h.Version == "" {
		return fmt.Errorf("%s does not answer like a topcoat hub (GET /api/health: HTTP %d)", base, resp.StatusCode)
	}
	return nil
}

func explain(base string, err error) error {
	var unknown x509.UnknownAuthorityError
	var invalid x509.CertificateInvalidError
	if errors.As(err, &unknown) || errors.As(err, &invalid) {
		return fmt.Errorf("the certificate of %s is not signed by the CA this node pinned when it paired: "+
			"move the hub together with its tls directory (same CA), or pair the node again: %w", base, err)
	}
	return hubtls.Explain(fmt.Errorf("reach %s: %w", base, err))
}

// pinnedFingerprint is the fingerprint of the first certificate in caPEM, as the hub GUI shows it.
func pinnedFingerprint(caPEM []byte) string {
	for {
		var blk *pem.Block
		blk, caPEM = pem.Decode(caPEM)
		if blk == nil {
			return ""
		}
		if blk.Type == "CERTIFICATE" {
			return hubtls.Fingerprint(blk.Bytes)
		}
	}
}
