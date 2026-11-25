package store

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sync"

	"topcoat/node/internal/protocol"
)

// DefaultQueueCap is the maximum number of events kept while offline (oldest dropped first).
const DefaultQueueCap = 1000

// Queue is a persistent FIFO of event envelopes stored as JSON lines.
// It is safe for concurrent use.
type Queue struct {
	mu      sync.Mutex
	path    string
	cap     int
	events  []protocol.Envelope
	dropped int
}

// OpenQueue loads the queue from path. Lines that cannot be parsed are skipped
// and reported via the returned skipped count.
func OpenQueue(path string, capacity int) (*Queue, int, error) {
	if capacity <= 0 {
		capacity = DefaultQueueCap
	}
	q := &Queue{path: path, cap: capacity}
	f, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return q, 0, nil
	}
	if err != nil {
		return nil, 0, fmt.Errorf("open queue: %w", err)
	}
	skipped, err := q.read(f)
	// Close before a rewrite: Windows cannot replace a file that is still open.
	_ = f.Close()
	if err != nil {
		return nil, skipped, err
	}
	if len(q.events) > q.cap {
		q.dropped += len(q.events) - q.cap
		q.events = q.events[len(q.events)-q.cap:]
		if err := q.rewriteLocked(); err != nil {
			return nil, skipped, err
		}
	} else if skipped > 0 {
		if err := q.rewriteLocked(); err != nil {
			return nil, skipped, err
		}
	}
	return q, skipped, nil
}

// read loads the events from f and returns the number of skipped lines.
func (q *Queue) read(f *os.File) (int, error) {
	skipped := 0
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for sc.Scan() {
		line := bytes.TrimSpace(sc.Bytes())
		if len(line) == 0 {
			continue
		}
		var e protocol.Envelope
		if err := json.Unmarshal(line, &e); err != nil || e.Type != protocol.TypeEvent || e.ID == "" {
			skipped++
			continue
		}
		q.events = append(q.events, e)
	}
	if err := sc.Err(); err != nil {
		return skipped, fmt.Errorf("read queue: %w", err)
	}
	return skipped, nil
}

// Append adds an event; if the queue is full the oldest event is dropped.
func (q *Queue) Append(e protocol.Envelope) error {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.events = append(q.events, e)
	if len(q.events) > q.cap {
		n := len(q.events) - q.cap
		q.dropped += n
		q.events = append([]protocol.Envelope(nil), q.events[n:]...)
		return q.rewriteLocked()
	}
	line, err := json.Marshal(e)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(q.path, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return fmt.Errorf("open queue for append: %w", err)
	}
	if _, err := f.Write(append(line, '\n')); err != nil {
		_ = f.Close()
		return fmt.Errorf("append queue: %w", err)
	}
	return f.Close()
}

// Snapshot returns a copy of the queued events, oldest first.
func (q *Queue) Snapshot() []protocol.Envelope {
	q.mu.Lock()
	defer q.mu.Unlock()
	return append([]protocol.Envelope(nil), q.events...)
}

// Len returns the number of queued events.
func (q *Queue) Len() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return len(q.events)
}

// Dropped returns how many events were dropped because the queue was full.
func (q *Queue) Dropped() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.dropped
}

// DropFirst removes the n oldest events (after they were delivered).
func (q *Queue) DropFirst(n int) error {
	q.mu.Lock()
	defer q.mu.Unlock()
	if n > len(q.events) {
		n = len(q.events)
	}
	if n <= 0 {
		return nil
	}
	q.events = append([]protocol.Envelope(nil), q.events[n:]...)
	return q.rewriteLocked()
}

func (q *Queue) rewriteLocked() error {
	var buf bytes.Buffer
	for _, e := range q.events {
		line, err := json.Marshal(e)
		if err != nil {
			return err
		}
		buf.Write(line)
		buf.WriteByte('\n')
	}
	return WriteFileAtomic(q.path, buf.Bytes())
}
