package agent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/coder/websocket"

	"topcoat/node/internal/hubtls"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/version"
)

// maxMessageBytes bounds incoming messages (configs are small).
const maxMessageBytes = 4 << 20

// unauthorizedError is returned when the hub rejects the node's signature during the handshake.
type unauthorizedError struct{ status int }

func (e *unauthorizedError) Error() string {
	return fmt.Sprintf("hub rejected the node's identity (HTTP %d); pair again if the hub was reset", e.status)
}

// session is one WebSocket connection to the hub.
type session struct {
	conn         *websocket.Conn
	writeTimeout time.Duration
	writeMu      sync.Mutex
	// readOnly is set after the hub answered upgrade_required: only heartbeats are sent.
	readOnly   atomic.Bool
	ready      atomic.Bool
	hbOnce     sync.Once
	done       chan struct{}
	actionsWG  sync.WaitGroup
	closedOnce sync.Once
	// streams cancels running gateway streams by stream id (E1-05).
	streamsMu sync.Mutex
	streams   map[string]context.CancelFunc
}

// streamIDs lists the gateway requests in flight on this session.
func (s *session) streamIDs() []string {
	s.streamsMu.Lock()
	defer s.streamsMu.Unlock()
	ids := make([]string, 0, len(s.streams))
	for id := range s.streams {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

func (s *session) write(env protocol.Envelope) error {
	b, err := json.Marshal(env)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), s.writeTimeout)
	defer cancel()
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	return s.conn.Write(ctx, websocket.MessageText, b)
}

// WSURL converts the hub base URL into the node WebSocket URL.
func WSURL(hub string) (string, error) {
	u, err := url.Parse(strings.TrimSpace(hub))
	if err != nil {
		return "", fmt.Errorf("parse hub url: %w", err)
	}
	switch u.Scheme {
	case "http", "ws":
		u.Scheme = "ws"
	case "https", "wss":
		u.Scheme = "wss"
	default:
		return "", fmt.Errorf("hub url %q: scheme must be http or https", hub)
	}
	if u.Host == "" {
		return "", fmt.Errorf("hub url %q has no host", hub)
	}
	u.Path = strings.TrimRight(u.Path, "/") + "/api/node/ws"
	u.RawQuery = ""
	u.Fragment = ""
	return u.String(), nil
}

func (a *Agent) dial(ctx context.Context) (*websocket.Conn, error) {
	wsURL, err := WSURL(a.opts.Identity.HubURL)
	if err != nil {
		return nil, err
	}
	dctx, cancel := context.WithTimeout(ctx, a.opts.DialTimeout)
	defer cancel()
	nonce, err := a.challenge(dctx)
	if err != nil {
		return nil, err
	}
	hdr := http.Header{}
	hdr.Set("Authorization", protocol.AuthorizationHeader(a.opts.Key, a.opts.Identity.NodeID, nonce))
	// On success resp.Body is the hijacked connection owned by conn and must not
	// be closed here; on failure the library has already buffered the body.
	conn, resp, err := websocket.Dial(dctx, wsURL, &websocket.DialOptions{ //nolint:bodyclose // see above
		HTTPHeader: hdr,
		HTTPClient: a.opts.HTTPClient,
	})
	if err != nil {
		if resp != nil && (resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden) {
			return nil, &unauthorizedError{status: resp.StatusCode}
		}
		return nil, fmt.Errorf("dial %s: %w", wsURL, hubtls.Explain(err))
	}
	conn.SetReadLimit(maxMessageBytes)
	return conn, nil
}

// challenge fetches a single-use nonce for the WebSocket authentication.
func (a *Agent) challenge(ctx context.Context) (string, error) {
	endpoint, err := ChallengeURL(a.opts.Identity.HubURL)
	if err != nil {
		return "", err
	}
	body, err := json.Marshal(protocol.ChallengeRequest{NodeID: a.opts.Identity.NodeID})
	if err != nil {
		return "", err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.opts.HTTPClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("challenge %s: %w", endpoint, hubtls.Explain(err))
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("challenge %s: HTTP %d", endpoint, resp.StatusCode)
	}
	var cr protocol.ChallengeResponse
	if err := json.NewDecoder(io.LimitReader(resp.Body, 64<<10)).Decode(&cr); err != nil {
		return "", fmt.Errorf("challenge %s: %w", endpoint, err)
	}
	if cr.Nonce == "" || strings.ContainsAny(cr.Nonce, ". ") {
		return "", fmt.Errorf("challenge %s: malformed nonce", endpoint)
	}
	return cr.Nonce, nil
}

// ChallengeURL is the nonce endpoint for a hub base URL.
func ChallengeURL(hub string) (string, error) {
	u, err := url.Parse(strings.TrimSpace(hub))
	if err != nil {
		return "", fmt.Errorf("parse hub url: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", fmt.Errorf("hub url %q: scheme must be https", hub)
	}
	u.Path = strings.TrimRight(u.Path, "/") + "/api/node/challenge"
	u.RawQuery, u.Fragment = "", ""
	return u.String(), nil
}

// runSession runs one connection. established reports whether hello/config
// completed (used to reset the reconnect backoff).
func (a *Agent) runSession(ctx context.Context) (established bool, err error) {
	conn, err := a.dial(ctx)
	if err != nil {
		return false, err
	}
	s := &session{conn: conn, writeTimeout: a.opts.WriteTimeout, done: make(chan struct{})}
	a.log.Info("connected to hub", "hub", a.opts.Identity.HubURL)

	closerDone := make(chan struct{})
	go func() {
		defer close(closerDone)
		select {
		case <-ctx.Done():
			// Graceful shutdown: normal closure.
			_ = conn.Close(websocket.StatusNormalClosure, "node shutting down")
		case <-s.done:
		}
	}()
	defer func() {
		a.emitMu.Lock()
		if a.live == s {
			a.live = nil
		}
		a.emitMu.Unlock()
		s.closedOnce.Do(func() { close(s.done) })
		<-closerDone
		_ = conn.CloseNow()
		s.cancelAllStreams()
		s.actionsWG.Wait()
	}()

	// Hello.
	_, hash, _ := a.config()
	hello := protocol.Hello{
		NodeID:          a.opts.Identity.NodeID,
		AgentVersion:    a.opts.AgentVersion,
		ProtocolVersion: version.ProtocolVersion,
	}
	if hash != "" {
		hello.ConfigHash = &hash
	}
	env, err := protocol.New(protocol.TypeHello, hello, a.opts.Now())
	if err != nil {
		return false, err
	}
	if err := s.write(env); err != nil {
		return false, fmt.Errorf("send hello: %w", err)
	}

	hsTimer := time.AfterFunc(a.opts.HandshakeTimeout, func() {
		if !s.ready.Load() && !s.readOnly.Load() {
			a.log.Warn("no config from hub within handshake timeout; reconnecting", "timeout", a.opts.HandshakeTimeout)
			_ = conn.Close(websocket.StatusPolicyViolation, "handshake timeout")
		}
	})
	defer hsTimer.Stop()

	readCtx, cancelRead := context.WithCancel(context.Background())
	defer cancelRead()
	for {
		typ, data, err := conn.Read(readCtx)
		if err != nil {
			if ctx.Err() != nil {
				return s.ready.Load(), ctx.Err()
			}
			return s.ready.Load(), fmt.Errorf("read: %w", err)
		}
		if typ != websocket.MessageText {
			a.log.Warn("ignoring non-text message from hub")
			continue
		}
		var in protocol.Envelope
		if err := json.Unmarshal(data, &in); err != nil {
			a.log.Warn("ignoring malformed message from hub", "err", err)
			continue
		}
		if in.V != version.ProtocolVersion {
			a.log.Warn("ignoring message with unsupported protocol version", "v", in.V, "type", in.Type)
			continue
		}
		switch in.Type {
		case protocol.TypeConfig:
			if !a.handleConfig(in) {
				continue
			}
			if s.ready.Load() && !s.readOnly.Load() {
				// a config change while the session runs can change what the node offers (the
				// capability registry is rebuilt from config.tools): tell the hub now, not only at
				// the next connect, or a new capability stays invisible until the service restarts
				a.bg.Add(1)
				go func() { defer a.bg.Done(); a.sendInventory(ctx) }()
			}
			if !s.ready.Load() {
				if err := a.finishHandshake(ctx, s); err != nil {
					return false, err
				}
				s.ready.Store(true)
				a.log.Info("session established")
				if !s.readOnly.Load() {
					a.confirmUpdate()
				}
			}
		case protocol.TypeAction:
			if s.readOnly.Load() {
				a.log.Warn("ignoring action: hub requires an agent upgrade", "id", in.ID)
				continue
			}
			s.actionsWG.Add(1)
			go func(env protocol.Envelope) {
				defer s.actionsWG.Done()
				a.handleAction(ctx, s, env)
			}(in)
		case protocol.TypeProxyRequest:
			if !s.ready.Load() || s.readOnly.Load() {
				continue
			}
			a.startProxy(ctx, s, in)
		case protocol.TypeProxyCancel:
			s.cancelStream(in)
		case protocol.TypeError:
			a.handleHubError(ctx, s, in)
		default:
			a.log.Warn("ignoring unexpected message type from hub", "type", in.Type)
		}
	}
}

// finishHandshake sends inventory, replays queued events and starts heartbeats.
func (a *Agent) finishHandshake(ctx context.Context, s *session) error {
	if !s.readOnly.Load() {
		inv, err := a.inventory(ctx)
		if err != nil {
			return err
		}
		if err := s.write(inv); err != nil {
			return fmt.Errorf("send inventory: %w", err)
		}
	}
	a.emitMu.Lock()
	if !s.readOnly.Load() {
		if evs := a.queue.Snapshot(); len(evs) > 0 {
			env, err := protocol.New(protocol.TypeQueuedEvents, protocol.QueuedEvents{Events: evs}, a.opts.Now())
			if err != nil {
				a.emitMu.Unlock()
				return err
			}
			if err := s.write(env); err != nil {
				a.emitMu.Unlock()
				return fmt.Errorf("send queued events: %w", err)
			}
			if err := a.queue.DropFirst(len(evs)); err != nil {
				a.log.Error("clear event queue", "err", err)
			}
			a.log.Info("replayed queued events", "count", len(evs))
		}
	}
	a.live = s
	a.emitMu.Unlock()
	a.startHeartbeats(ctx, s)
	return nil
}

// startHeartbeats sends one heartbeat now and then every interval until the session ends.
func (a *Agent) startHeartbeats(ctx context.Context, s *session) {
	s.hbOnce.Do(func() {
		a.sendHeartbeat(ctx)
		go func() {
			for {
				t := time.NewTimer(a.heartbeatInterval())
				select {
				case <-s.done:
					t.Stop()
					return
				case <-ctx.Done():
					t.Stop()
					return
				case <-t.C:
					a.sendHeartbeat(ctx)
				}
			}
		}()
	})
}

// handleConfig applies a config message; returns false if it was rejected.
func (a *Agent) handleConfig(env protocol.Envelope) bool {
	var body protocol.ConfigBody
	if err := env.DecodeBody(&body); err != nil {
		a.log.Error("invalid config message", "err", err)
		return false
	}
	computed, err := protocol.ConfigHash(body.Config)
	if err != nil {
		a.log.Error("invalid config JSON", "err", err)
		return false
	}
	if computed != body.ConfigHash {
		a.log.Warn("config_hash mismatch (hub and node canonical JSON differ); using the hub's hash",
			"hub_hash", body.ConfigHash, "node_hash", computed)
	}
	cfg, err := protocol.ParseNodeConfig(body.Config)
	if err != nil {
		a.log.Error("rejecting config from hub", "err", err)
		a.emit(protocol.EventError, map[string]any{"message": "config rejected: " + err.Error()})
		return false
	}
	a.applyConfig(cfg, body.ConfigHash)
	if err := a.opts.Dir.SaveConfig(body.ConfigHash, body.Config, a.opts.Now()); err != nil {
		a.log.Error("cache config", "err", err)
	}
	a.log.Info("config applied", "config_hash", body.ConfigHash, "runner", cfg.Runner.Type,
		"allowed_actions", len(cfg.AllowedActions), "watcher_rules", len(cfg.Watcher.Rules))
	return true
}

func (a *Agent) handleHubError(ctx context.Context, s *session, env protocol.Envelope) {
	var e protocol.Error
	if err := env.DecodeBody(&e); err != nil {
		a.log.Warn("malformed error message from hub", "err", err)
		return
	}
	if e.Code == protocol.ErrUpgradeRequired {
		a.log.Error("hub requires a newer agent; staying connected but only sending heartbeats",
			"message", e.Message, "agent_version", a.opts.AgentVersion)
		s.readOnly.Store(true)
		a.emitMu.Lock()
		a.live = s
		a.emitMu.Unlock()
		a.startHeartbeats(ctx, s)
		return
	}
	a.log.Warn("hub reported an error", "code", e.Code, "message", e.Message, "re", env.Re)
}

// errIsUnauthorized reports whether err is a rejected node identity (for callers/tests).
func errIsUnauthorized(err error) bool {
	var ue *unauthorizedError
	return errors.As(err, &ue)
}
