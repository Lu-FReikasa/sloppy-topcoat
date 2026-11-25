// Package watcher defines the ProcessWatcher interface and the real,
// polling implementation.
package watcher

import (
	"context"
	"path/filepath"
	"strings"
	"time"
)

// Rule matches a process by executable name or by a folder prefix of its
// executable path, both case-insensitive. A rule with both fields set matches
// if either matches.
type Rule struct {
	Process    string
	PathPrefix string
}

// Matches reports whether the executable name / path matches the rule.
func (r Rule) Matches(exe, path string) bool {
	if r.Process != "" && strings.EqualFold(r.Process, exe) {
		return true
	}
	if r.PathPrefix != "" && path != "" {
		return hasPathPrefix(path, r.PathPrefix)
	}
	return false
}

// hasPathPrefix compares case-insensitively and treats / and \ alike, so a
// Windows rule like `D:\SteamLibrary\steamapps\common` matches
// `d:/steamlibrary/steamapps/common/Game/game.exe`.
func hasPathPrefix(path, prefix string) bool {
	norm := func(s string) string { return strings.ToLower(strings.ReplaceAll(s, `\`, "/")) }
	p, pre := norm(path), strings.TrimRight(norm(prefix), "/")
	if pre == "" {
		return false
	}
	return p == pre || strings.HasPrefix(p, pre+"/")
}

// Event reports that a watched process started or ended.
type Event struct {
	// Process is the executable name (e.g. game.exe).
	Process string
	// Path is the full executable path if known.
	Path    string
	Started bool
}

// Key identifies a process instance group for start/end bookkeeping: the
// lowercased path if known, else the lowercased name.
func (e Event) Key() string {
	if e.Path != "" {
		return strings.ToLower(e.Path)
	}
	return strings.ToLower(e.Process)
}

// ProcessWatcher detects configured game executables / GPU-heavy processes.
type ProcessWatcher interface {
	// Watch reports matching processes starting and ending until ctx is done;
	// the channel is closed then. Processes already running when Watch starts
	// are reported as started.
	Watch(ctx context.Context, rules []Rule) (<-chan Event, error)
}

// Proc is one entry of a process listing.
type Proc struct {
	Name string // executable file name
	Path string // full executable path, "" if not accessible
}

// Lister returns the current process list.
type Lister func() ([]Proc, error)

// DefaultInterval is the polling interval of the system watcher.
const DefaultInterval = 2 * time.Second

// System watches real OS processes by polling the process list.
//
// Windows: CreateToolhelp32Snapshot + QueryFullProcessImageNameW (no CGO).
// Linux: /proc/<pid>/exe and /proc/<pid>/comm.
type System struct {
	List     Lister
	Interval time.Duration
	// Tick is injectable for tests; default time.NewTicker(Interval).C.
	Tick func(ctx context.Context) <-chan time.Time
}

// NewSystem returns the process watcher for this OS.
func NewSystem() *System { return &System{List: listProcesses, Interval: DefaultInterval} }

// Watch implements ProcessWatcher.
func (s *System) Watch(ctx context.Context, rules []Rule) (<-chan Event, error) {
	// Fail early (e.g. no permission to list processes) instead of silently.
	first, err := s.List()
	if err != nil {
		return nil, err
	}
	rules = append([]Rule(nil), rules...)
	tick := s.Tick
	if tick == nil {
		iv := s.Interval
		if iv <= 0 {
			iv = DefaultInterval
		}
		tick = func(ctx context.Context) <-chan time.Time {
			t := time.NewTicker(iv)
			go func() { <-ctx.Done(); t.Stop() }()
			return t.C
		}
	}
	ch := make(chan Event, 16)
	go func() {
		defer close(ch)
		running := map[string]Event{}
		send := func(ev Event) bool {
			select {
			case ch <- ev:
				return true
			case <-ctx.Done():
				return false
			}
		}
		poll := func(procs []Proc) bool {
			now := matching(procs, rules)
			for k, ev := range now {
				if _, ok := running[k]; !ok {
					running[k] = ev
					if !send(ev) {
						return false
					}
				}
			}
			for k, ev := range running {
				if _, ok := now[k]; !ok {
					delete(running, k)
					ev.Started = false
					if !send(ev) {
						return false
					}
				}
			}
			return true
		}
		if !poll(first) {
			return
		}
		t := tick(ctx)
		for {
			select {
			case <-ctx.Done():
				return
			case <-t:
				procs, err := s.List()
				if err != nil {
					continue // transient; keep the last known state
				}
				if !poll(procs) {
					return
				}
			}
		}
	}()
	return ch, nil
}

// matching returns the processes matching any rule, keyed by Event.Key.
func matching(procs []Proc, rules []Rule) map[string]Event {
	out := map[string]Event{}
	for _, p := range procs {
		name := p.Name
		if name == "" && p.Path != "" {
			name = filepath.Base(strings.ReplaceAll(p.Path, `\`, "/"))
		}
		for _, r := range rules {
			if r.Matches(name, p.Path) {
				ev := Event{Process: name, Path: p.Path, Started: true}
				out[ev.Key()] = ev
				break
			}
		}
	}
	return out
}
