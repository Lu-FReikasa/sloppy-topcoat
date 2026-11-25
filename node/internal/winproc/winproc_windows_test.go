package winproc

import (
	"os"
	"strings"
	"testing"

	"golang.org/x/sys/windows"
)

func TestOwnProcess(t *testing.T) {
	pid := uint32(os.Getpid()) //nolint:gosec // a pid fits 32 bits
	var want uint32
	if err := windows.ProcessIdToSessionId(pid, &want); err != nil {
		t.Fatal(err)
	}
	if got, err := SessionOf(pid); err != nil || got != want {
		t.Fatalf("session %d, %v; want %d", got, err, want)
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	if got, err := ImagePath(pid); err != nil || !strings.EqualFold(got, self) {
		t.Fatalf("image %q, %v; want %q", got, err, self)
	}
}
