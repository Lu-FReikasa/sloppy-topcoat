// Package backoff implements capped exponential backoff with jitter.
package backoff

import (
	"math/rand/v2"
	"time"
)

// Backoff yields delays Min, 2*Min, 4*Min ... capped at Max, each reduced by up to
// Jitter (fraction 0..1) at random so that many nodes do not reconnect in lockstep.
// It is not safe for concurrent use.
type Backoff struct {
	Min    time.Duration
	Max    time.Duration
	Jitter float64
	// Rand returns a value in [0,1); nil uses math/rand/v2.
	Rand func() float64

	attempt int
}

// Default returns the reconnect backoff: 1s doubling up to 60s, 20% jitter.
func Default() *Backoff {
	return &Backoff{Min: time.Second, Max: 60 * time.Second, Jitter: 0.2}
}

// Base returns the un-jittered delay for the next attempt.
func (b *Backoff) Base() time.Duration {
	d := b.Min
	for i := 0; i < b.attempt && d < b.Max; i++ {
		d *= 2
	}
	if d > b.Max {
		d = b.Max
	}
	return d
}

// Next returns the delay before the next attempt and advances the attempt counter.
func (b *Backoff) Next() time.Duration {
	d := b.Base()
	if b.Base() < b.Max {
		b.attempt++
	}
	return b.jitter(d)
}

// MaxDelay returns a jittered maximum delay without changing the attempt counter
// (used after fatal-ish errors such as a rejected node identity).
func (b *Backoff) MaxDelay() time.Duration {
	return b.jitter(b.Max)
}

// Reset starts over at Min.
func (b *Backoff) Reset() { b.attempt = 0 }

func (b *Backoff) jitter(d time.Duration) time.Duration {
	if b.Jitter <= 0 {
		return d
	}
	r := b.Rand
	if r == nil {
		r = rand.Float64 //nolint:gosec // jitter does not need a CSPRNG
	}
	return d - time.Duration(float64(d)*b.Jitter*r())
}
