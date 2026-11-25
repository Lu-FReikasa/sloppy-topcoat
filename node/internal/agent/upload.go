package agent

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"topcoat/node/internal/hubtls"
	"topcoat/node/internal/protocol"
)

// UploadArtifact sends one finished file of a build to the hub with the node's own credentials
// (a single-use signed nonce, like the update download). The build service has no key and no
// route to the hub: it asks the node to do this, and the node checks the file first (see
// cmd/topcoat-node). The hub accepts it only from the node the build was given to.
func (a *Agent) UploadArtifact(ctx context.Context, buildID, name, osName, arch string, body io.Reader, size int64) error {
	base, err := url.Parse(strings.TrimSpace(a.opts.Identity.HubURL))
	if err != nil || (base.Scheme != "http" && base.Scheme != "https") {
		return fmt.Errorf("hub url %q is not valid", a.opts.Identity.HubURL)
	}
	base.Path = strings.TrimRight(base.Path, "/") + "/api/node/builds/" + url.PathEscape(buildID) + "/artifacts/" + url.PathEscape(name)
	base.RawQuery = url.Values{"os": {osName}, "arch": {arch}}.Encode()
	nonce, err := a.challenge(ctx)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, base.String(), body)
	if err != nil {
		return err
	}
	req.ContentLength = size
	req.Header.Set("Content-Type", "application/octet-stream")
	req.Header.Set("Authorization", protocol.AuthorizationHeader(a.opts.Key, a.opts.Identity.NodeID, nonce))
	resp, err := a.opts.HTTPClient.Do(req)
	if err != nil {
		return fmt.Errorf("upload %s: %w", name, hubtls.Explain(err))
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusNoContent && resp.StatusCode != http.StatusOK {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<10))
		return fmt.Errorf("upload %s: HTTP %d %s", name, resp.StatusCode, strings.TrimSpace(string(msg)))
	}
	return nil
}
