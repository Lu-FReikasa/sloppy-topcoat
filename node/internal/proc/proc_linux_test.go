package proc

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestParseVmRSS(t *testing.T) {
	if got := parseVMRSS([]byte("Name:\tx\nVmRSS:\t  204800 kB\nThreads: 3\n")); got != 200 {
		t.Fatalf("got %d", got)
	}
	if parseVMRSS([]byte("Name: x\n")) != 0 {
		t.Fatal("expected 0")
	}
}

func TestStartStopRealProcess(t *testing.T) {
	sleep, err := exec.LookPath("sleep")
	if err != nil {
		t.Skip("no sleep binary")
	}
	var out bytes.Buffer
	p, err := Start(context.Background(), sleep, []string{"30"}, "", &out)
	if err != nil {
		t.Fatal(err)
	}
	if p.PID() <= 0 || p.RSSMB() < 0 {
		t.Fatal("bad pid/rss")
	}
	start := time.Now()
	if err := p.Stop(5 * time.Second); err != nil {
		t.Fatal(err)
	}
	if time.Since(start) > 4*time.Second {
		t.Fatal("SIGTERM was not honored")
	}
	<-p.Done()
	if p.Err() == nil {
		t.Fatal("expected signal exit error")
	}
}

func TestStopOfAFinishedProcessIsNotAnError(t *testing.T) {
	sleep, err := exec.LookPath("sleep")
	if err != nil {
		t.Skip("no sleep binary")
	}
	p, err := Start(context.Background(), sleep, []string{"0"}, "", &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	<-p.Done()
	if err := p.Stop(time.Second); err != nil {
		t.Fatal(err)
	}
	// a process that ignores SIGTERM is killed after the timeout, and the kernel's teardown
	// after that is waited for instead of being reported as an error
	sh, err := exec.LookPath("sh")
	if err != nil {
		t.Skip("no sh")
	}
	stubborn, err := Start(context.Background(), sh, []string{"-c", "trap '' TERM; exec sleep 30"}, "", &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(200 * time.Millisecond)
	if err := stubborn.Stop(300 * time.Millisecond); err != nil {
		t.Fatalf("kill after the timeout: %v", err)
	}
	select {
	case <-stubborn.Done():
	default:
		t.Fatal("Stop returned before the process was gone")
	}
}

// procStat returns the state letter and the nice value of a process (fields 3 and 19 of
// /proc/<pid>/stat; the command name in parentheses may contain spaces).
func procStat(t *testing.T, pid int) (state string, nice int) {
	t.Helper()
	b, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		t.Fatal(err)
	}
	rest := string(b[bytes.LastIndexByte(b, ')')+2:])
	f := strings.Fields(rest)
	nice, _ = strconv.Atoi(f[16])
	return f[0], nice
}

func waitState(t *testing.T, pid int, want string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for {
		if s, _ := procStat(t, pid); s == want || (want != "T" && s != "T") {
			return
		}
		if time.Now().After(deadline) {
			s, _ := procStat(t, pid)
			t.Fatalf("state %s, want %s", s, want)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestSuspendAndResumeAreIdempotentAndKeepTheProcess(t *testing.T) {
	sleep, err := exec.LookPath("sleep")
	if err != nil {
		t.Skip("no sleep binary")
	}
	p, err := Start(context.Background(), sleep, []string{"30"}, "", &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = p.Stop(2 * time.Second) }()
	pid := p.PID()
	if err := p.Resume(); err != nil { // resuming a running process does nothing
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ { // a second Suspend must not fail or stack
		if err := p.Suspend(); err != nil {
			t.Fatal(err)
		}
	}
	waitState(t, pid, "T")
	if err := p.Resume(); err != nil {
		t.Fatal(err)
	}
	waitState(t, pid, "S") // one Resume is enough after two Suspends
	select {
	case <-p.Done():
		t.Fatal("freezing must not end the process")
	default:
	}
}

func TestStopEndsASuspendedProcess(t *testing.T) {
	sleep, err := exec.LookPath("sleep")
	if err != nil {
		t.Skip("no sleep binary")
	}
	p, err := Start(context.Background(), sleep, []string{"30"}, "", &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Suspend(); err != nil {
		t.Fatal(err)
	}
	waitState(t, p.PID(), "T")
	start := time.Now()
	if err := p.Stop(5 * time.Second); err != nil {
		t.Fatal(err)
	}
	if time.Since(start) > 3*time.Second {
		t.Fatal("SIGTERM must reach a stopped process (Stop continues it first)")
	}
}

func TestProcessesStartAtTheLowestPriority(t *testing.T) {
	sleep, err := exec.LookPath("sleep")
	if err != nil {
		t.Skip("no sleep binary")
	}
	defer SetLowPriority(true)
	for _, c := range []struct {
		low  bool
		nice int
	}{{true, 19}, {false, 0}} {
		SetLowPriority(c.low)
		p, err := Start(context.Background(), sleep, []string{"30"}, "", &bytes.Buffer{})
		if err != nil {
			t.Fatal(err)
		}
		_, nice := procStat(t, p.PID())
		_ = p.Stop(2 * time.Second)
		if nice != c.nice {
			t.Errorf("low=%v: nice %d, want %d", c.low, nice, c.nice)
		}
	}
}

func TestKillEndsAProcessThatIgnoresSIGTERMAtOnce(t *testing.T) {
	sh, err := exec.LookPath("sh")
	if err != nil {
		t.Skip("no sh")
	}
	// like a llama-server that is busy loading: it does not react to SIGTERM
	p, err := Start(context.Background(), sh, []string{"-c", "trap '' TERM; while true; do sleep 1; done"}, "", &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(100 * time.Millisecond)
	start := time.Now()
	if err := p.Kill(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-p.Done():
	case <-time.After(3 * time.Second):
		t.Fatal("Kill did not end the process")
	}
	if time.Since(start) > 2*time.Second {
		t.Fatal("too slow")
	}
	if err := p.Kill(); err != nil { // a second Kill of a finished process is fine
		t.Fatal(err)
	}
}
