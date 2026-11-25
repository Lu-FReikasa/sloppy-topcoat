package fake

import (
	"context"
	"encoding/json"
	"strings"
	"sync"

	"topcoat/node/internal/providers/watcher"
)

// Watcher is a ProcessWatcher driven by Start / Stop calls (from tests or the
// fake CLI mode).
type Watcher struct {
	mu      sync.Mutex
	running map[string]watcher.Event // Event.Key -> start event
	subs    map[*sub]struct{}
}

type sub struct {
	ctx   context.Context
	rules []watcher.Rule
	ch    chan watcher.Event
}

// NewWatcher returns a fake watcher with no running processes.
func NewWatcher() *Watcher {
	return &Watcher{running: map[string]watcher.Event{}, subs: map[*sub]struct{}{}}
}

// Watch implements watcher.ProcessWatcher.
func (w *Watcher) Watch(ctx context.Context, rules []watcher.Rule) (<-chan watcher.Event, error) {
	s := &sub{ctx: ctx, rules: append([]watcher.Rule(nil), rules...), ch: make(chan watcher.Event, 64)}
	w.mu.Lock()
	w.subs[s] = struct{}{}
	for _, ev := range w.running {
		w.deliver(s, ev)
	}
	w.mu.Unlock()
	go func() {
		<-ctx.Done()
		w.mu.Lock()
		delete(w.subs, s)
		close(s.ch)
		w.mu.Unlock()
	}()
	return s.ch, nil
}

// deliver sends ev to s if it matches; must be called with w.mu held.
func (w *Watcher) deliver(s *sub, ev watcher.Event) {
	match := false
	for _, r := range s.rules {
		if r.Matches(ev.Process, ev.Path) {
			match = true
			break
		}
	}
	if !match {
		return
	}
	select {
	case s.ch <- ev:
	case <-s.ctx.Done():
	}
}

// Start simulates a process starting (executable name only).
func (w *Watcher) Start(process string) { w.StartPath(process, "") }

// StartPath simulates a process starting with a known executable path.
func (w *Watcher) StartPath(process, path string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	ev := watcher.Event{Process: process, Path: path, Started: true}
	if _, ok := w.running[ev.Key()]; ok {
		return
	}
	w.running[ev.Key()] = ev
	for s := range w.subs {
		w.deliver(s, ev)
	}
}

// Stop simulates a process ending (by name, or by path for StartPath).
func (w *Watcher) Stop(processOrPath string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	key := strings.ToLower(processOrPath)
	ev, ok := w.running[key]
	if !ok {
		return
	}
	delete(w.running, key)
	ev.Started = false
	for s := range w.subs {
		w.deliver(s, ev)
	}
}

// Subscribers returns the number of active Watch calls (for tests).
func (w *Watcher) Subscribers() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return len(w.subs)
}

// Echo is a fake capability that returns its input.
type Echo struct{}

// Name implements capability.Capability.
func (Echo) Name() string { return "fake.echo" }

// InputSchema implements capability.Capability.
func (Echo) InputSchema() json.RawMessage { return json.RawMessage(`{"type":"object"}`) }

// Invoke implements capability.Capability.
func (Echo) Invoke(_ context.Context, in json.RawMessage) (json.RawMessage, error) {
	return in, nil
}
