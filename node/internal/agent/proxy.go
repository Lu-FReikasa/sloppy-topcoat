package agent

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/runner"
)

// ActionModelsProxy is the permission for gateway requests (E1-05): a node
// forwards proxy_request messages only if it is in allowed_actions.
const ActionModelsProxy = "models.proxy"

const (
	// maxProxyBody bounds a forwarded request body.
	maxProxyBody = 1 << 20
	// proxyChunkBytes is the most response data per proxy_chunk (schema: 64 KiB).
	proxyChunkBytes = 32 << 10
	// maxProxyStreams bounds concurrent gateway streams per session.
	maxProxyStreams = 16
)

// proxyIdleTimeout ends a stream when the model sends nothing for this long.
var proxyIdleTimeout = 5 * time.Minute

// proxyPaths are the only upstream endpoints the gateway may reach, with the
// method each accepts.
var proxyPaths = map[string]string{
	"/v1/chat/completions": http.MethodPost,
	"/v1/completions":      http.MethodPost,
	"/v1/embeddings":       http.MethodPost,
	"/v1/models":           http.MethodGet,
}

func (s *session) addStream(id string, cancel context.CancelFunc) bool {
	s.streamsMu.Lock()
	defer s.streamsMu.Unlock()
	if s.streams == nil {
		s.streams = map[string]context.CancelFunc{}
	}
	if _, dup := s.streams[id]; dup || len(s.streams) >= maxProxyStreams {
		return false
	}
	s.streams[id] = cancel
	return true
}

func (s *session) removeStream(id string) {
	s.streamsMu.Lock()
	delete(s.streams, id)
	s.streamsMu.Unlock()
}

func (s *session) cancelStream(env protocol.Envelope) {
	var c protocol.ProxyCancel
	if err := env.DecodeBody(&c); err != nil {
		return
	}
	s.streamsMu.Lock()
	cancel := s.streams[c.StreamID]
	s.streamsMu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (s *session) cancelAllStreams() {
	s.streamsMu.Lock()
	defer s.streamsMu.Unlock()
	for _, cancel := range s.streams {
		cancel()
	}
}

// startProxy validates a proxy_request and forwards it in the background.
func (a *Agent) startProxy(ctx context.Context, s *session, env protocol.Envelope) {
	var req protocol.ProxyRequest
	if err := env.DecodeBody(&req); err != nil || req.StreamID == "" {
		a.log.Warn("malformed proxy_request", "err", err)
		return
	}
	end := func(status int, code, msg string) {
		a.sendProxy(s, protocol.TypeProxyEnd, protocol.ProxyEnd{StreamID: req.StreamID, Status: status, Error: errorf(code, "%s", msg)})
	}
	cfg, _, r := a.config()
	switch {
	case cfg == nil || !cfg.ActionAllowed(ActionModelsProxy):
		end(http.StatusForbidden, protocol.ErrNotAllowed, "models.proxy is not allowed on this node")
		return
	case proxyPaths[req.Path] == "" || proxyPaths[req.Path] != req.Method:
		end(http.StatusForbidden, protocol.ErrNotAllowed, req.Method+" "+req.Path+" is not forwarded")
		return
	case len(req.Body) > maxProxyBody:
		end(http.StatusRequestEntityTooLarge, protocol.ErrInvalidArgs, "request body too large")
		return
	case a.isPaused():
		// The person at this machine comes first (E2-05 B): the hub asks another node.
		end(http.StatusServiceUnavailable, protocol.ErrBusy, "node is paused for local use")
		return
	case a.isGaming() || a.press.Active(a.opts.Now()):
		// The GPU belongs to someone else right now (E1-06): the hub tries another node.
		end(http.StatusServiceUnavailable, protocol.ErrBusy, "node is busy gaming or has yielded its VRAM")
		return
	}
	px, ok := r.(runner.Proxier)
	if r == nil || !ok {
		end(http.StatusNotImplemented, protocol.ErrNotImplemented, "the runner cannot answer model requests")
		return
	}
	target, err := px.ProxyTarget(ctx, req.Model)
	if err != nil {
		if errors.Is(err, runner.ErrNotLoaded) {
			end(http.StatusConflict, protocol.ErrNotLoaded, "model "+req.Model+" is not loaded")
		} else {
			end(http.StatusBadGateway, protocol.ErrRunnerError, err.Error())
		}
		return
	}
	sctx, cancel := context.WithCancel(ctx)
	if !s.addStream(req.StreamID, cancel) {
		cancel()
		end(http.StatusTooManyRequests, protocol.ErrBusy, "too many concurrent requests on this node")
		return
	}
	s.actionsWG.Add(1)
	go func() {
		defer s.actionsWG.Done()
		defer s.removeStream(req.StreamID)
		defer cancel()
		a.forward(sctx, s, req, target)
	}()
}

// forward sends the request upstream and relays the response as chunks.
func (a *Agent) forward(ctx context.Context, s *session, req protocol.ProxyRequest, target runner.ProxyTarget) {
	a.touch(req.Model)
	defer a.touch(req.Model)
	end := func(status int, perr *protocol.Error) {
		a.sendProxy(s, protocol.TypeProxyEnd, protocol.ProxyEnd{StreamID: req.StreamID, Status: status, Error: perr})
	}
	var body io.Reader = http.NoBody
	if req.Method == http.MethodPost {
		body = bytes.NewReader(req.Body)
	}
	hreq, err := http.NewRequestWithContext(ctx, req.Method, target.BaseURL+req.Path, body)
	if err != nil {
		end(http.StatusBadGateway, errorf(protocol.ErrInternal, "%v", err))
		return
	}
	for _, h := range []string{"Content-Type", "Accept"} {
		for k, v := range req.Headers {
			if http.CanonicalHeaderKey(k) == h {
				hreq.Header.Set(h, v)
			}
		}
	}
	if target.APIKey != "" {
		hreq.Header.Set("Authorization", "Bearer "+target.APIKey)
	}
	// Idle watchdog: cancel when nothing arrives for proxyIdleTimeout.
	wctx, stop := context.WithCancelCause(ctx)
	defer stop(nil)
	hreq = hreq.WithContext(wctx)
	var idle *time.Timer
	idle = time.AfterFunc(proxyIdleTimeout, func() {
		if a.isPaused() { // frozen for local use: silence is expected, not a hung model
			idle.Reset(proxyIdleTimeout)
			return
		}
		stop(errProxyIdle)
	})
	defer idle.Stop()

	resp, err := target.Client.Do(hreq)
	if err != nil {
		if ctx.Err() != nil {
			return // canceled by the hub: nobody waits for an answer
		}
		code := protocol.ErrRunnerError
		if errors.Is(context.Cause(wctx), errProxyIdle) {
			code = protocol.ErrTimeout
		}
		end(http.StatusBadGateway, errorf(code, "model %s: %v", req.Model, err))
		return
	}
	defer func() { _ = resp.Body.Close() }()
	buf := make([]byte, proxyChunkBytes)
	seq := 0
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			idle.Reset(proxyIdleTimeout)
			chunk := protocol.ProxyChunk{StreamID: req.StreamID, Seq: seq, Data: append([]byte(nil), buf[:n]...)}
			if seq == 0 {
				chunk.Status, chunk.ContentType = resp.StatusCode, resp.Header.Get("Content-Type")
			}
			if !a.sendProxy(s, protocol.TypeProxyChunk, chunk) {
				return // session gone; the deferred cancel stops the upstream request
			}
			seq++
		}
		if rerr == io.EOF {
			if ctx.Err() == nil { // a canceled stream gets no proxy_end
				end(resp.StatusCode, nil)
			}
			return
		}
		if rerr != nil {
			if ctx.Err() != nil {
				return
			}
			code := protocol.ErrRunnerError
			switch {
			case errors.Is(context.Cause(wctx), errProxyIdle):
				code = protocol.ErrTimeout
			case !a.modelLoaded(ctx, req.Model):
				// The node itself ended the model (a long pause, memory needed by another
				// program, a manual unload): the hub can continue the answer later.
				code = protocol.ErrInterrupted
			}
			end(resp.StatusCode, errorf(code, "model %s: %v", req.Model, rerr))
			return
		}
	}
}

var errProxyIdle = errors.New("no data from the model")

// modelLoaded reports whether the runner still has model loaded.
func (a *Agent) modelLoaded(ctx context.Context, model string) bool {
	_, _, r := a.config()
	if r == nil {
		return false
	}
	loaded, err := r.Loaded(context.WithoutCancel(ctx))
	if err != nil {
		return true // cannot tell: not an unload
	}
	for _, l := range loaded {
		if l.Name == model {
			return true
		}
	}
	return false
}

// sendProxy writes a proxy message on the session the request came from.
func (a *Agent) sendProxy(s *session, typ string, body any) bool {
	env, err := protocol.New(typ, body, a.opts.Now())
	if err != nil {
		a.log.Error("build proxy message", "err", err)
		return false
	}
	if err := s.write(env); err != nil {
		a.log.Debug("send proxy message", "err", err)
		return false
	}
	return true
}
