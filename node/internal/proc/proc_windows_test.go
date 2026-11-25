package proc

import (
	"bytes"
	"context"
	"os/exec"
	"testing"
	"time"
)

func TestStartStopWindows(t *testing.T) {
	ping, err := exec.LookPath("ping")
	if err != nil {
		t.Skip("no ping")
	}
	var out bytes.Buffer
	p, err := Start(context.Background(), ping, []string{"-n", "30", "127.0.0.1"}, "", &out)
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(300 * time.Millisecond)
	if p.RSSMB() <= 0 {
		t.Errorf("rss %d", p.RSSMB())
	}
	if err := p.Stop(5 * time.Second); err != nil {
		t.Fatal(err)
	}
	select {
	case <-p.Done():
	case <-time.After(5 * time.Second):
		t.Fatal("process did not stop")
	}
}

// TestStopOfATerminatingProcessWindows: TerminateProcess on a process that is already being
// torn down answers ERROR_ACCESS_DENIED; Stop must treat that as "gone" (a 30B model takes
// seconds to release its memory, and unload all failed with "Zugriff verweigert").
func TestStopOfATerminatingProcessWindows(t *testing.T) {
	ping, err := exec.LookPath("ping")
	if err != nil {
		t.Skip("no ping")
	}
	p, err := Start(context.Background(), ping, []string{"-n", "30", "127.0.0.1"}, "", &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(200 * time.Millisecond)
	op := p.(*osProcess)
	_ = op.cmd.Process.Kill() // the process is now terminating (or gone)
	if err := p.Stop(50 * time.Millisecond); err != nil {
		t.Fatalf("Stop after a kill: %v", err)
	}
	select {
	case <-p.Done():
	case <-time.After(5 * time.Second):
		t.Fatal("process did not stop")
	}
}
