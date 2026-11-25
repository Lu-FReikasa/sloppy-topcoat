package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"runtime"
	"strings"
	"time"

	"topcoat/node/internal/hubtls"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/update"
)

// UpdateOptions switches on the update channel of the node (E2-06, D50). Nil in Options: the node
// neither stages updates nor reports update state.
type UpdateOptions struct {
	// Keys are the public keys of this build; none means "cannot verify, so cannot update".
	Keys []update.Key
	// Plat is this machine's platform (default: runtime.GOOS / runtime.GOARCH).
	Plat update.Platform
	// Source builds the download source; nil = the hub (HubSource). Tests inject a fake.
	Source func(a *Agent) update.Source
	// StaleAfter is how old the updater's heartbeat may be before it counts as stale
	// (default 60 s).
	StaleAfter time.Duration
	// Timeout bounds one staging (default 20 min).
	Timeout time.Duration
	// StateDir is the updater's own directory (heartbeat and result), owned by the updater's
	// account; empty = inside the node's update directory (tests, development).
	StateDir string
	// ResultPoll is how often the node looks for the updater's result (default 10 s).
	ResultPoll time.Duration
}

// ActionNodeUpdate stages a signed build for the updater.
const ActionNodeUpdate = "node.update"

func (a *Agent) updateEnabled() bool { return a.opts.Update != nil }

func (a *Agent) updateLayout() update.Layout {
	return update.NewLayout(a.opts.Dir.Path).WithState(a.opts.Update.StateDir)
}

func (a *Agent) updatePlat() update.Platform {
	if p := a.opts.Update.Plat; p.OS != "" {
		return p
	}
	return update.Platform{OS: runtime.GOOS, Arch: runtime.GOARCH}
}

// updaterState reads the privileged updater's heartbeat.
func (a *Agent) updaterState() string {
	st := a.updateLayout().ReadStatus()
	if st == nil {
		return protocol.UpdaterMissing
	}
	at, err := time.Parse(time.RFC3339, st.AliveAt)
	if err != nil {
		return protocol.UpdaterMissing
	}
	stale := a.opts.Update.StaleAfter
	if stale <= 0 {
		stale = 60 * time.Second
	}
	if a.opts.Now().Sub(at) > stale {
		return protocol.UpdaterStale
	}
	return protocol.UpdaterOK
}

// updateInfo is the update section of the inventory.
func (a *Agent) updateInfo() *protocol.UpdateInfo {
	if !a.updateEnabled() {
		return nil
	}
	info := &protocol.UpdateInfo{
		Capable: len(a.opts.Update.Keys) > 0,
		Updater: a.updaterState(),
		KeyIDs:  update.KeyIDs(a.opts.Update.Keys),
	}
	if st := a.updateLayout().ReadStatus(); st != nil {
		info.UpdaterVersion = st.Version
	}
	a.updaterSeen.Store(info.Updater + "|" + info.UpdaterVersion)
	if cfg, _, _ := a.config(); cfg != nil {
		info.Channel = cfg.UpdateChannel()
	}
	return info
}

// reportUpdateResult tells the hub how the last update ended (the updater leaves result.json
// for the node). Events queue until the hub is connected. It returns whether there was a result.
func (a *Agent) reportUpdateResult() bool {
	res := a.updateLayout().TakeResult()
	if res == nil {
		return false
	}
	if res.OK {
		a.log.Info("update applied", "version", res.Version)
		a.emit(protocol.EventUpdateApplied, map[string]any{"version": res.Version})
		return true
	}
	a.log.Warn("update failed", "version", res.Version, "reason", res.Reason)
	a.emit(protocol.EventUpdateFailed, map[string]any{"version": res.Version, "reason": res.Reason})
	return true
}

// updateReportLoop (after the first look in Run) looks for the updater's result now and then: the updater writes it only after
// the new (or restored) node is up and has been confirmed, so it is usually not there yet when
// the node starts.
func (a *Agent) updateReportLoop(ctx context.Context) {
	every := a.opts.Update.ResultPoll
	if every <= 0 {
		every = 10 * time.Second
	}
	for sleepCtx(ctx, every) == nil {
		a.reportUpdateResult()
		a.resendOnNewUpdater(ctx)
	}
}

// resendOnNewUpdater sends the inventory again when the updater's state (ok, stale, missing) or the
// version in its heartbeat differs from what the last inventory said. After an update the new node
// connects a few seconds before the updater restarts with the new program, and an updater that
// stopped only turns stale a minute later; without this the hub kept what the node said at connect
// (live 2026-11-09: "updater 0.2.5, ok" next to node 0.2.7 while the updater was stopped).
func (a *Agent) resendOnNewUpdater(ctx context.Context) {
	seen, ok := a.updaterSeen.Load().(string)
	if !ok {
		return // no inventory was sent yet: the first one reports the heartbeat as it is
	}
	now := a.updaterState() + "|"
	if st := a.updateLayout().ReadStatus(); st != nil {
		now += st.Version
	}
	if now != seen {
		a.sendInventory(ctx)
	}
}

// confirmUpdate records that this node, in this version, reached the hub. The updater waits for
// it after it started a new program, and rolls back without it.
func (a *Agent) confirmUpdate() {
	if !a.updateEnabled() {
		return
	}
	if err := a.updateLayout().WriteConfirmed(a.opts.AgentVersion, a.opts.Now()); err != nil {
		a.log.Warn("write the update confirmation", "err", err)
	}
}

func (a *Agent) actNodeUpdate(_ context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args, "version"); perr != nil {
		return nil, perr, nil
	}
	if !a.updateEnabled() || len(a.opts.Update.Keys) == 0 {
		return nil, errorf(protocol.ErrNotImplemented, "this build of the node carries no update key"), nil
	}
	version := ""
	if _, present := args["version"]; present {
		v, ok := argString(args, "version")
		if !ok {
			return nil, errorf(protocol.ErrInvalidArgs, "args.version must be a string"), nil
		}
		if _, err := update.ParseVersion(v); err != nil {
			return nil, errorf(protocol.ErrInvalidArgs, "%v", err), nil
		}
		version = v
	}
	cfg, _, _ := a.config()
	if cfg == nil {
		return nil, errorf(protocol.ErrRunnerError, "no config yet"), nil
	}
	if st := a.updaterState(); st != protocol.UpdaterOK {
		return nil, errorf(protocol.ErrNotImplemented,
			"the updater service is %s on this machine: install the program once by hand (topcoat-node install) so that it registers the updater", st), nil
	}
	if !a.updating.CompareAndSwap(false, true) {
		return nil, errorf(protocol.ErrBusy, "an update is being staged already"), nil
	}
	channel := cfg.UpdateChannel()
	return map[string]any{"started": true, "channel": channel}, nil, func() {
		a.bg.Add(1)
		go func() {
			defer a.bg.Done()
			defer a.updating.Store(false)
			a.stageUpdate(a.jobCtx(), channel, version)
		}()
	}
}

func (a *Agent) stageUpdate(ctx context.Context, channel, version string) {
	timeout := a.opts.Update.Timeout
	if timeout <= 0 {
		timeout = 20 * time.Minute
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	a.emit(protocol.EventUpdateStaging, map[string]any{"channel": channel, "version": version})
	src := update.Source(&HubSource{Agent: a})
	if a.opts.Update.Source != nil {
		src = a.opts.Update.Source(a)
	}
	st := &update.Stager{
		Keys: a.opts.Update.Keys, Layout: a.updateLayout(), Source: src, Plat: a.updatePlat(),
		Installed: a.opts.AgentVersion, Channel: channel, Now: a.opts.Now,
	}
	m, err := st.Stage(ctx, version)
	if err != nil {
		a.log.Warn("update not staged", "err", err)
		a.emit(protocol.EventUpdateFailed, map[string]any{"version": version, "reason": err.Error()})
		return
	}
	a.log.Info("update staged", "version", m.Version, "channel", m.Channel)
	a.emit(protocol.EventUpdateStaged, map[string]any{"version": m.Version, "channel": m.Channel})
}

// HubSource fetches builds from the hub over the node's own TLS client, authenticated like the
// WebSocket: a single-use nonce signed with the node key.
type HubSource struct{ Agent *Agent }

// EndpointURL builds a hub URL below /api/node/update.
func EndpointURL(hub, name string, query url.Values) (string, error) {
	u, err := url.Parse(strings.TrimSpace(hub))
	if err != nil {
		return "", fmt.Errorf("parse hub url: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", fmt.Errorf("hub url %q: scheme must be https", hub)
	}
	u.Path = strings.TrimRight(u.Path, "/") + "/api/node/update/" + name
	u.RawQuery, u.Fragment = query.Encode(), ""
	return u.String(), nil
}

func (s *HubSource) get(ctx context.Context, name string, query url.Values) (*http.Response, error) {
	a := s.Agent
	endpoint, err := EndpointURL(a.opts.Identity.HubURL, name, query)
	if err != nil {
		return nil, err
	}
	nonce, err := a.challenge(ctx)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", protocol.AuthorizationHeader(a.opts.Key, a.opts.Identity.NodeID, nonce))
	resp, err := a.opts.HTTPClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", name, hubtls.Explain(err))
	}
	if resp.StatusCode != http.StatusOK {
		defer func() { _ = resp.Body.Close() }()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<10))
		return nil, fmt.Errorf("%s: HTTP %d %s", name, resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return resp, nil
}

func versionQuery(version string) url.Values {
	q := url.Values{}
	if version != "" {
		q.Set("version", version)
	}
	return q
}

// Manifest implements update.Source.
func (s *HubSource) Manifest(ctx context.Context, version string) ([]byte, string, error) {
	resp, err := s.get(ctx, "manifest", versionQuery(version))
	if err != nil {
		return nil, "", err
	}
	defer func() { _ = resp.Body.Close() }()
	var body struct {
		Manifest  string `json:"manifest"`
		Signature string `json:"signature"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, update.MaxManifestBytes*4)).Decode(&body); err != nil {
		return nil, "", fmt.Errorf("manifest: %w", err)
	}
	if body.Manifest == "" || body.Signature == "" {
		return nil, "", errors.New("manifest: the hub sent no manifest or signature")
	}
	return []byte(body.Manifest), body.Signature, nil
}

// Binary implements update.Source.
func (s *HubSource) Binary(ctx context.Context, version string, dst io.Writer, limit int64) error {
	resp, err := s.get(ctx, "binary", versionQuery(version))
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	n, err := io.Copy(dst, io.LimitReader(resp.Body, limit))
	if err != nil {
		return err
	}
	if n >= limit {
		return fmt.Errorf("the program is larger than %d bytes", limit-1)
	}
	return nil
}
