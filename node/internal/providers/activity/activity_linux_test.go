package activity

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestParseProcStatAndProcessTicks(t *testing.T) {
	total, busy, ok := parseProcStat([]byte("cpu  100 0 50 800 40 5 5 0 0 0\ncpu0 1 2 3\n"))
	if !ok || total != 1000 || busy != 160 { // idle = 800 + 40 iowait
		t.Fatalf("%v %v %v", total, busy, ok)
	}
	if _, _, ok := parseProcStat([]byte("garbage")); ok {
		t.Fatal("garbage accepted")
	}
	// the command name has spaces and a parenthesis; utime = 300, stime = 20
	stat := "4242 (llama (server) x) S 1 4242 4242 0 -1 4194560 100 0 0 0 300 20 0 0 20 0 8 0 1000 1000 1000 18446744073709551615"
	if got, ok := parseProcessTicks([]byte(stat)); !ok || got != 320 {
		t.Fatalf("%v %v", got, ok)
	}
	if _, ok := parseProcessTicks([]byte("4242 (x) S 1")); ok {
		t.Fatal("short line accepted")
	}
}

func writeProc(t *testing.T, root string, total, busy, own int) {
	t.Helper()
	idle := total - busy
	if err := os.WriteFile(filepath.Join(root, "stat"), []byte("cpu  "+itoa(busy)+" 0 0 "+itoa(idle)+" 0 0 0 0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_ = os.MkdirAll(filepath.Join(root, "77"), 0o755)
	if err := os.WriteFile(filepath.Join(root, "77", "stat"), []byte("77 (llama-server) S 1 1 1 0 -1 0 0 0 0 0 "+itoa(own)+" 0 0 0 20 0 1 0 1 1 1"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func itoa(n int) string { return strconv.Itoa(n) }

func TestForeignCPUExcludesTheNodesOwnProcesses(t *testing.T) {
	root := t.TempDir()
	s := &system{proc: root, power: t.TempDir()}
	writeProc(t, root, 1000, 100, 10)
	if _, known := s.cpu([]int{77}); known {
		t.Fatal("the first sample has no delta")
	}
	// 1000 more jiffies pass, 700 of them busy, 300 of these are the model's
	writeProc(t, root, 2000, 800, 310)
	got, known := s.cpu([]int{77})
	if !known || got != 40 {
		t.Fatalf("foreign %v (known %v), want 40", got, known)
	}
	// with nothing of ours in it the same busy time is all foreign
	writeProc(t, root, 3000, 1500, 310)
	if got, _ := s.cpu(nil); got != 70 {
		t.Fatalf("foreign %v, want 70", got)
	}
}

func writeSupply(t *testing.T, root, name string, files map[string]string) {
	t.Helper()
	dir := filepath.Join(root, name)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	for k, v := range files {
		if err := os.WriteFile(filepath.Join(dir, k), []byte(v+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func TestBatteryState(t *testing.T) {
	root := t.TempDir()
	if _, known := batteryState(root); known {
		t.Fatal("no supply at all: unknown")
	}
	writeSupply(t, root, "AC", map[string]string{"type": "Mains", "online": "1"})
	if _, known := batteryState(root); known {
		t.Fatal("a desktop (mains only) is never 'on battery'")
	}
	writeSupply(t, root, "BAT0", map[string]string{"type": "Battery", "status": "Charging"})
	if on, known := batteryState(root); on || !known {
		t.Fatalf("plugged in and charging: %v %v", on, known)
	}
	writeSupply(t, root, "AC", map[string]string{"type": "Mains", "online": "0"})
	writeSupply(t, root, "BAT0", map[string]string{"type": "Battery", "status": "Discharging"})
	if on, known := batteryState(root); !on || !known {
		t.Fatalf("unplugged: %v %v", on, known)
	}
	// a wireless mouse's battery is not the machine's
	writeSupply(t, root, "hidpp_battery_0", map[string]string{"type": "Battery", "scope": "Device", "status": "Discharging"})
	writeSupply(t, root, "BAT0", map[string]string{"type": "Battery", "status": "Full"})
	writeSupply(t, root, "AC", map[string]string{"type": "Mains", "online": "1"})
	if on, _ := batteryState(root); on {
		t.Fatal("the mouse battery must be ignored")
	}
}

func TestIdleFromLogindSessions(t *testing.T) {
	now := 1000 * time.Second
	active := parseSession([]byte("Active=yes\nRemote=no\nIdleHint=no\nIdleSinceHintMonotonic=0\n"))
	idle := parseSession([]byte("Active=yes\nRemote=no\nIdleHint=yes\nIdleSinceHintMonotonic=940000000\n"))
	if !idle.idleHint || idle.idleSince != 940*time.Second {
		t.Fatalf("%+v", idle)
	}
	if d, known := idleFromSessions([]logindSession{active}, now); !known || d != 0 {
		t.Fatalf("session in use: %v %v", d, known)
	}
	if d, known := idleFromSessions([]logindSession{idle}, now); !known || d != 60*time.Second {
		t.Fatalf("idle since 940 s: %v %v", d, known)
	}
	remote := parseSession([]byte("Active=yes\nRemote=yes\nIdleHint=no\n"))
	background := parseSession([]byte("Active=no\nRemote=no\nIdleHint=no\n"))
	if _, known := idleFromSessions([]logindSession{remote, background}, now); known {
		t.Fatal("remote and inactive sessions are nobody at this machine")
	}
	if _, known := idleFromSessions(nil, now); known {
		t.Fatal("no session (headless): unknown")
	}
	if d, _ := idleFromSessions([]logindSession{idle, active}, now); d != 0 {
		t.Fatalf("one active session wins: %v", d)
	}
}

func TestInputThroughLoginctl(t *testing.T) {
	calls := 0
	s := &system{
		mono: func() time.Duration { return 500 * time.Second },
		loginc: func(_ context.Context, args ...string) ([]byte, error) {
			calls++
			if args[0] == "list-sessions" {
				return []byte("  2 1000 user seat0 tty2\n c1 0 root - -\n"), nil
			}
			if args[1] == "2" {
				return []byte("Active=yes\nRemote=no\nIdleHint=yes\nIdleSinceHintMonotonic=470000000\n"), nil
			}
			return []byte("Active=no\nRemote=no\nIdleHint=no\n"), nil
		},
	}
	idle, known, why := s.input(context.Background())
	if !known || idle != 30*time.Second || why != "" {
		t.Fatalf("%v %v %q", idle, known, why)
	}
	s.loginc = func(context.Context, ...string) ([]byte, error) { return nil, errors.New("not found") }
	if _, known, why := s.input(context.Background()); known || !strings.Contains(why, "loginctl") {
		t.Fatalf("missing loginctl must be said once: %v %q", known, why)
	}
}

func TestSystemProviderPutsTheSignalsTogether(t *testing.T) {
	s := NewSystem()
	got := s.Sample(context.Background(), nil) // on the test machine: must not panic, whatever it knows
	if got.CPUKnown {
		t.Fatal("the first sample has no CPU delta")
	}
}
