package agent

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	"topcoat/node/internal/providers/fake"
)

func TestAnArtifactIsUploadedWithASignedNonce(t *testing.T) {
	hub := newFakeHub(t)
	var path, query, method, ctype string
	var got []byte
	var length int64
	hub.builds = func(w http.ResponseWriter, r *http.Request) {
		path, query, method, ctype, length = r.URL.EscapedPath(), r.URL.RawQuery, r.Method, r.Header.Get("Content-Type"), r.ContentLength
		got, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusNoContent)
	}
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	body := "linux program"
	err := h.agent.UploadArtifact(context.Background(), "11111111-2222-4333-8444-555555555555", "topcoat-node-linux-amd64", "linux", "amd64", strings.NewReader(body), int64(len(body)))
	if err != nil {
		t.Fatal(err)
	}
	if method != http.MethodPut || path != "/api/node/builds/11111111-2222-4333-8444-555555555555/artifacts/topcoat-node-linux-amd64" || query != "arch=amd64&os=linux" {
		t.Fatalf("%s %s?%s", method, path, query)
	}
	if string(got) != body || length != int64(len(body)) || ctype != "application/octet-stream" {
		t.Fatalf("body=%q length=%d type=%q", got, length, ctype)
	}
}

func TestAnUploadTheHubRefusesIsAnErrorWithTheReason(t *testing.T) {
	hub := newFakeHub(t)
	hub.builds = func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"detail":{"code":"not_building"}}`, http.StatusConflict)
	}
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile())
	err := h.agent.UploadArtifact(context.Background(), "b", "x", "linux", "amd64", strings.NewReader("x"), 1)
	if err == nil || !strings.Contains(err.Error(), "409") || !strings.Contains(err.Error(), "not_building") {
		t.Fatalf("got %v", err)
	}
}
