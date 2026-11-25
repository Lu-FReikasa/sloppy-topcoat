package backoff

import (
	"testing"
	"time"
)

func TestSequenceAndCap(t *testing.T) {
	b := Default()
	b.Jitter = 0
	want := []time.Duration{1, 2, 4, 8, 16, 32, 60, 60, 60}
	for i, w := range want {
		if got := b.Next(); got != w*time.Second {
			t.Fatalf("attempt %d: got %v want %v", i, got, w*time.Second)
		}
	}
	b.Reset()
	if got := b.Next(); got != time.Second {
		t.Fatalf("after reset: got %v", got)
	}
}

func TestJitterBounds(t *testing.T) {
	b := &Backoff{Min: time.Second, Max: 60 * time.Second, Jitter: 0.2, Rand: func() float64 { return 0.999999 }}
	d := b.Next()
	if d > time.Second || d < 800*time.Millisecond {
		t.Fatalf("jittered delay %v out of [0.8s,1s]", d)
	}
	b.Rand = func() float64 { return 0 }
	if d := b.Next(); d != 2*time.Second {
		t.Fatalf("got %v", d)
	}
	if d := b.MaxDelay(); d != 60*time.Second {
		t.Fatalf("max delay %v", d)
	}
	// Default random source stays within bounds.
	b2 := Default()
	for i := 0; i < 100; i++ {
		d := b2.Next()
		if d <= 0 || d > 60*time.Second {
			t.Fatalf("delay %v out of range", d)
		}
	}
}
