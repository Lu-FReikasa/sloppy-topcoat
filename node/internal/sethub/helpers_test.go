package sethub

import (
	"net/http/httptest"
	"runtime"
	"testing"
)

func isWindows() bool { return runtime.GOOS == "windows" }

// httptestHub is a plain-http hub.
func httptestHub(t *testing.T) string {
	t.Helper()
	s := httptest.NewServer(hubHandler())
	t.Cleanup(s.Close)
	return s.URL
}
