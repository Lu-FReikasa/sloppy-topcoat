package fake

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"topcoat/node/internal/providers/runner"
)

// ProxyTarget implements runner.Proxier: loaded fake models answer
// OpenAI-style requests through an in-process handler (no listening socket).
func (f fakeRunner) ProxyTarget(_ context.Context, name string) (runner.ProxyTarget, error) {
	f.w.mu.Lock()
	_, ok := f.w.loaded[name]
	f.w.mu.Unlock()
	if !ok {
		return runner.ProxyTarget{}, fmt.Errorf("%w: %s", runner.ErrNotLoaded, name)
	}
	h := &chatHandler{w: f.w, model: name}
	return runner.ProxyTarget{BaseURL: "http://fake-llama-server", Client: &http.Client{Transport: handlerTransport{h}}, APIKey: f.w.APIKey}, nil
}

// chatHandler imitates llama-server's OpenAI endpoints: the answer repeats the
// last user message, streamed word by word with World.StreamDelay.
type chatHandler struct {
	w     *World
	model string
}

type chatRequest struct {
	Model         string `json:"model"`
	Stream        bool   `json:"stream"`
	StreamOptions struct {
		IncludeUsage bool `json:"include_usage"`
	} `json:"stream_options"`
	Prompt   string `json:"prompt"`
	Messages []struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	} `json:"messages"`
}

func (h *chatHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if key := h.w.APIKey; key != "" && r.Header.Get("Authorization") != "Bearer "+key {
		http.Error(w, `{"error":{"message":"Invalid API Key","type":"authentication_error","code":401}}`, http.StatusUnauthorized)
		return
	}
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/v1/models":
		writeJSON(w, http.StatusOK, map[string]any{"object": "list", "data": []map[string]any{{"id": h.model, "object": "model", "owned_by": "llamacpp"}}})
		return
	case r.Method != http.MethodPost:
		writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"error": map[string]any{"message": "method not allowed"}})
		return
	}
	var req chatRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 4<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": map[string]any{"message": "invalid JSON", "type": "invalid_request_error"}})
		return
	}
	switch r.URL.Path {
	case "/v1/embeddings":
		vec := make([]float64, 8)
		for i := range vec {
			vec[i] = float64(i+len(h.model)) / 10
		}
		writeJSON(w, http.StatusOK, map[string]any{"object": "list", "model": h.model, "data": []map[string]any{{"object": "embedding", "index": 0, "embedding": vec}}})
		return
	case "/v1/chat/completions", "/v1/completions":
	default:
		writeJSON(w, http.StatusNotFound, map[string]any{"error": map[string]any{"message": "not found"}})
		return
	}
	prompt := req.Prompt
	for _, m := range req.Messages {
		if m.Role == "user" {
			prompt = m.Content
		}
	}
	answer := "Fake-Antwort von " + h.model + ": " + prompt
	chat := r.URL.Path == "/v1/chat/completions"
	if !req.Stream {
		choice := map[string]any{"index": 0, "finish_reason": "stop", "text": answer}
		object := "text_completion"
		if chat {
			choice = map[string]any{"index": 0, "finish_reason": "stop", "message": map[string]any{"role": "assistant", "content": answer}}
			object = "chat.completion"
		}
		writeJSON(w, http.StatusOK, map[string]any{"id": "fake-1", "object": object, "created": time.Now().Unix(), "model": h.model,
			"choices": []any{choice}, "usage": map[string]any{"prompt_tokens": len(strings.Fields(prompt)), "completion_tokens": len(strings.Fields(answer))}})
		return
	}
	h.w.mu.Lock()
	delay := h.w.StreamDelay
	h.w.mu.Unlock()
	w.Header().Set("Content-Type", "text/event-stream")
	w.WriteHeader(http.StatusOK)
	words := strings.Fields(answer)
	for i, word := range words {
		if i > 0 {
			word = " " + word
		}
		choice := map[string]any{"index": 0, "text": word, "finish_reason": nil}
		if chat {
			choice = map[string]any{"index": 0, "delta": map[string]any{"content": word}, "finish_reason": nil}
		}
		if !writeEvent(w, map[string]any{"id": "fake-1", "object": "chat.completion.chunk", "model": h.model, "choices": []any{choice}}) {
			return
		}
		select {
		case <-r.Context().Done():
			return
		case <-time.After(delay):
		}
		if !h.w.waitThawed(r.Context()) {
			return
		}
		if !h.w.isLoaded(h.model) {
			// like a llama-server that was stopped: the connection breaks mid-answer
			if ab, ok := w.(interface{ Abort(error) }); ok {
				ab.Abort(io.ErrUnexpectedEOF)
			}
			return
		}
	}
	final := map[string]any{"index": 0, "delta": map[string]any{}, "finish_reason": "stop"}
	writeEvent(w, map[string]any{"id": "fake-1", "object": "chat.completion.chunk", "model": h.model, "choices": []any{final}})
	if req.StreamOptions.IncludeUsage {
		writeEvent(w, map[string]any{"id": "fake-1", "object": "chat.completion.chunk", "model": h.model, "choices": []any{},
			"usage": map[string]any{"prompt_tokens": len(strings.Fields(prompt)), "completion_tokens": len(words)}})
	}
	_, _ = io.WriteString(w, "data: [DONE]\n\n")
}

func writeEvent(w http.ResponseWriter, v any) bool {
	b, _ := json.Marshal(v)
	if _, err := fmt.Fprintf(w, "data: %s\n\n", b); err != nil {
		return false
	}
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// handlerTransport serves requests with an http.Handler through a pipe, so
// streamed responses arrive while the handler still writes.
type handlerTransport struct{ h http.Handler }

func (t handlerTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	pr, pw := io.Pipe()
	rw := &pipeResponse{header: http.Header{}, pw: pw, ready: make(chan struct{})}
	go func() {
		defer func() {
			rw.WriteHeader(http.StatusOK)
			_ = pw.CloseWithError(rw.abortErr) // nil = a normal end
		}()
		t.h.ServeHTTP(rw, req)
	}()
	select {
	case <-rw.ready:
	case <-req.Context().Done():
		_ = pr.CloseWithError(req.Context().Err())
		return nil, req.Context().Err()
	}
	return &http.Response{Status: http.StatusText(rw.status), StatusCode: rw.status, Proto: "HTTP/1.1", ProtoMajor: 1, ProtoMinor: 1,
		Header: rw.header, Body: pr, Request: req, ContentLength: -1}, nil
}

type pipeResponse struct {
	header   http.Header
	pw       *io.PipeWriter
	once     sync.Once
	status   int
	ready    chan struct{}
	abortErr error
}

// Abort makes the response end with err instead of a clean end of file.
func (p *pipeResponse) Abort(err error) { p.abortErr = err }

func (p *pipeResponse) Header() http.Header { return p.header }

func (p *pipeResponse) WriteHeader(status int) {
	p.once.Do(func() {
		p.status = status
		close(p.ready)
	})
}

func (p *pipeResponse) Write(b []byte) (int, error) {
	p.WriteHeader(http.StatusOK)
	return p.pw.Write(b)
}

func (p *pipeResponse) Flush() {}
