package agent

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/fake"
	"topcoat/node/internal/providers/runner"
)

const proxyConfig = `{"roles":["gpu"],"runner":{"type":"fake"},` +
	`"allowed_actions":["models.list","models.load","models.unload","models.proxy"],` +
	`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,"watcher":{"rules":[{"process":"game.exe"}]},"capabilities":[]}`

const streamID = "7b1e9f00-0000-4000-8000-00000000abcd"

func chatBody(stream bool) []byte {
	b, _ := json.Marshal(map[string]any{"model": "fake-llm-3b-q8", "stream": stream,
		"messages": []map[string]string{{"role": "user", "content": "Hallo Welt"}}})
	return b
}

func proxyReq(id, path, method string, body []byte) protocol.ProxyRequest {
	if body == nil {
		body = []byte{}
	}
	return protocol.ProxyRequest{StreamID: id, Model: "fake-llm-3b-q8", Method: method, Path: path,
		Headers: map[string]string{"content-type": "application/json"}, Body: body}
}

// collect reads proxy_chunk messages of one stream until proxy_end.
func collect(t *testing.T, hc *hubConn, id string) ([]protocol.ProxyChunk, protocol.ProxyEnd) {
	t.Helper()
	var chunks []protocol.ProxyChunk
	for {
		env := hc.waitFor(t, "proxy messages", func(e protocol.Envelope) bool {
			return e.Type == protocol.TypeProxyChunk || e.Type == protocol.TypeProxyEnd
		})
		if env.Type == protocol.TypeProxyEnd {
			var end protocol.ProxyEnd
			if err := env.DecodeBody(&end); err != nil {
				t.Fatal(err)
			}
			if end.StreamID == id {
				return chunks, end
			}
			continue
		}
		var c protocol.ProxyChunk
		if err := env.DecodeBody(&c); err != nil {
			t.Fatal(err)
		}
		if c.StreamID == id {
			chunks = append(chunks, c)
		}
	}
}

func proxySetup(t *testing.T, cfg string) (*harness, *hubConn) {
	t.Helper()
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.world.StreamDelay = 0
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, cfg)
	return h, hc
}

func TestProxyStreamsChatAnswer(t *testing.T) {
	_, hc := proxySetup(t, proxyConfig)
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res)
	}
	hc.send(t, protocol.TypeProxyRequest, proxyReq(streamID, "/v1/chat/completions", "POST", chatBody(true)))
	chunks, end := collect(t, hc, streamID)
	if end.Status != 200 || end.Error != nil || len(chunks) == 0 {
		t.Fatalf("end %+v, %d chunks", end, len(chunks))
	}
	if chunks[0].Status != 200 || chunks[0].ContentType != "text/event-stream" {
		t.Fatalf("first chunk %+v", chunks[0])
	}
	var all strings.Builder
	for i, c := range chunks {
		if c.Seq != i {
			t.Fatalf("seq %d at %d", c.Seq, i)
		}
		all.Write(c.Data)
	}
	if !strings.Contains(all.String(), `"content":" Welt"`) || !strings.HasSuffix(all.String(), "data: [DONE]\n\n") {
		t.Fatalf("stream %q", all.String())
	}

	// Non-streaming answer.
	id2 := "7b1e9f00-0000-4000-8000-00000000abce"
	hc.send(t, protocol.TypeProxyRequest, proxyReq(id2, "/v1/chat/completions", "POST", chatBody(false)))
	chunks, end = collect(t, hc, id2)
	if end.Status != 200 || len(chunks) == 0 || !strings.Contains(string(chunks[0].Data), "Fake-Antwort von fake-llm-3b-q8: Hallo Welt") {
		t.Fatalf("end %+v chunks %+v", end, chunks)
	}
}

func TestProxyRefusals(t *testing.T) {
	cfgNoProxy := strings.Replace(proxyConfig, `,"models.proxy"`, "", 1)
	cases := []struct {
		name, cfg, path, method string
		load, game              bool
		status                  int
		code                    string
	}{
		{"no permission", cfgNoProxy, "/v1/chat/completions", "POST", true, false, 403, protocol.ErrNotAllowed},
		{"path not forwarded", proxyConfig, "/v1/models", "POST", true, false, 403, protocol.ErrNotAllowed},
		{"model not loaded", proxyConfig, "/v1/chat/completions", "POST", false, false, 409, protocol.ErrNotLoaded},
		{"game running", proxyConfig, "/v1/chat/completions", "POST", true, true, 503, protocol.ErrBusy},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h, hc := proxySetup(t, tc.cfg)
			if tc.load {
				if _, err := h.world.Runner().Load(context.Background(), "fake-llm-3b-q8", runner.LoadOptions{}); err != nil {
					t.Fatal(err)
				}
			}
			if tc.game {
				h.watcher.Start("game.exe")
				hc.waitEvent(t, protocol.EventGameDetected)
				// the event is sent before the node counts as gaming (see handleProcessEvent)
				eventually(t, "busy_gaming", func() bool { return h.agent.State() == protocol.StateBusyGaming })
			}
			hc.send(t, protocol.TypeProxyRequest, proxyReq(streamID, tc.path, tc.method, chatBody(false)))
			chunks, end := collect(t, hc, streamID)
			if len(chunks) != 0 || end.Status != tc.status || end.Error == nil || end.Error.Code != tc.code {
				t.Fatalf("chunks %d end %+v", len(chunks), end)
			}
		})
	}
}

// proxy_cancel stops a stream: no proxy_end follows, the node is ready for more.
func TestProxyCancel(t *testing.T) {
	h, hc := proxySetup(t, proxyConfig)
	h.world.StreamDelay = 200 * time.Millisecond
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res)
	}
	hc.send(t, protocol.TypeProxyRequest, proxyReq(streamID, "/v1/chat/completions", "POST", chatBody(true)))
	hc.waitType(t, protocol.TypeProxyChunk)
	hc.send(t, protocol.TypeProxyCancel, protocol.ProxyCancel{StreamID: streamID})
	deadline := time.After(1500 * time.Millisecond)
	for {
		select {
		case env := <-hc.msgs:
			if env.Type == protocol.TypeProxyEnd {
				t.Fatalf("proxy_end after cancel: %s", env.Body)
			}
		case <-deadline:
			return
		}
	}
}

// A resident reload and a gateway load of the same model load it once, and
// the heartbeat reports it as loading meanwhile.
func TestConcurrentLoadsLoadOnce(t *testing.T) {
	h, hc := proxySetup(t, proxyConfig)
	h.world.LoadDelay = 300 * time.Millisecond
	load := map[string]any{"name": "models.load", "args": json.RawMessage(`{"model":"fake-llm-3b-q8"}`)}
	ids := map[string]bool{hc.send(t, protocol.TypeAction, load): true, hc.send(t, protocol.TypeAction, load): true}
	sawLoading := false
	for len(ids) > 0 {
		env := hc.next(t)
		if hb, ok := heartbeatState(t, env); ok && len(hb.Loading) == 1 && hb.Loading[0] == "fake-llm-3b-q8" {
			sawLoading = true
		}
		if env.Type == protocol.TypeActionResult && ids[env.Re] {
			var r protocol.ActionResult
			_ = env.DecodeBody(&r)
			if !r.OK {
				t.Fatalf("load: %+v", r)
			}
			delete(ids, env.Re)
		}
	}
	if !sawLoading {
		t.Fatal("no heartbeat reported the model as loading")
	}
	if h.world.Loads != 1 {
		t.Fatalf("model loaded %d times", h.world.Loads)
	}
}

func TestProxyCarriesTheServersKeyAndIgnoresTheHubsAuthorization(t *testing.T) {
	hub := newFakeHub(t)
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	h.world.StreamDelay = 0
	h.world.APIKey = "per-start-key"
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, proxyConfig)
	if res := hc.action(t, "models.load", `{"model":"fake-llm-3b-q8"}`); !res.OK {
		t.Fatalf("load: %+v", res)
	}
	req := proxyReq(streamID, "/v1/chat/completions", "POST", chatBody(false))
	req.Headers["authorization"] = "Bearer something-the-hub-made-up" // not forwarded
	hc.send(t, protocol.TypeProxyRequest, req)
	chunks, end := collect(t, hc, streamID)
	if end.Status != 200 || len(chunks) == 0 || chunks[0].Status != 200 {
		t.Fatalf("the node must authenticate itself to its llama-server: %+v %+v", end, chunks)
	}
}
