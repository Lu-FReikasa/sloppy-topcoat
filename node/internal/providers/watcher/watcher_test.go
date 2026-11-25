package watcher

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

func TestRuleMatches(t *testing.T) {
	r := Rule{Process: "game.exe"}
	if !r.Matches("GAME.EXE", "") || r.Matches("game.exe.bak", "") {
		t.Fatal("process rule")
	}
	p := Rule{PathPrefix: `D:\SteamLibrary\steamapps\common\`}
	if !p.Matches("x.exe", `d:\steamlibrary\SteamApps\common\Some Game\bin\x.exe`) {
		t.Fatal("path prefix should match case-insensitively")
	}
	if !p.Matches("x.exe", "D:/SteamLibrary/steamapps/common/G/x.exe") {
		t.Fatal("slashes should be normalized")
	}
	if p.Matches("x.exe", `D:\SteamLibrary\steamapps\commonX\x.exe`) || p.Matches("x.exe", "") {
		t.Fatal("prefix must end at a path boundary; empty path never matches")
	}
	if (Rule{PathPrefix: "/"}).Matches("x", "/usr/bin/x") {
		t.Fatal("root prefix must not match everything")
	}
}

type scripted struct {
	mu    sync.Mutex
	procs []Proc
	err   error
}

func (s *scripted) set(p ...Proc) { s.mu.Lock(); s.procs = p; s.mu.Unlock() }
func (s *scripted) list() ([]Proc, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]Proc(nil), s.procs...), s.err
}

func recv(t *testing.T, ch <-chan Event) Event {
	t.Helper()
	select {
	case ev := <-ch:
		return ev
	case <-time.After(2 * time.Second):
		t.Fatal("no event")
	}
	return Event{}
}

func TestSystemPollsAndDiffs(t *testing.T) {
	s := &scripted{}
	s.set(Proc{Name: "explorer.exe"}, Proc{Name: "game.exe", Path: `C:\Games\game.exe`})
	ticks := make(chan time.Time)
	w := &System{List: s.list, Tick: func(context.Context) <-chan time.Time { return ticks }}
	ctx, cancel := context.WithCancel(context.Background())
	ch, err := w.Watch(ctx, []Rule{{Process: "game.exe"}, {PathPrefix: `D:\Steam\common`}})
	if err != nil {
		t.Fatal(err)
	}
	if ev := recv(t, ch); !ev.Started || ev.Process != "game.exe" || ev.Path != `C:\Games\game.exe` {
		t.Fatalf("%+v", ev)
	}
	// A Steam game starts (matched by folder), game.exe keeps running.
	s.set(Proc{Name: "game.exe", Path: `C:\Games\game.exe`}, Proc{Path: `D:\Steam\common\Foo\foo.exe`})
	ticks <- time.Now()
	if ev := recv(t, ch); !ev.Started || ev.Process != "foo.exe" {
		t.Fatalf("%+v", ev)
	}
	// Listing errors keep the last state.
	s.mu.Lock()
	s.err = errors.New("transient")
	s.mu.Unlock()
	ticks <- time.Now()
	s.mu.Lock()
	s.err = nil
	s.mu.Unlock()
	s.set()
	ticks <- time.Now()
	got := map[string]bool{}
	for range 2 {
		ev := recv(t, ch)
		if ev.Started {
			t.Fatalf("%+v", ev)
		}
		got[ev.Process] = true
	}
	if !got["game.exe"] || !got["foo.exe"] {
		t.Fatalf("%v", got)
	}
	cancel()
	for range ch {
	}
}

func TestSystemFailsEarly(t *testing.T) {
	w := &System{List: func() ([]Proc, error) { return nil, errors.New("denied") }}
	if _, err := w.Watch(context.Background(), []Rule{{Process: "x"}}); err == nil {
		t.Fatal("expected error")
	}
}

func TestEventKey(t *testing.T) {
	if (Event{Process: "A.exe"}).Key() != "a.exe" || (Event{Process: "a", Path: `C:\X\A.EXE`}).Key() != `c:\x\a.exe` {
		t.Fatal("key")
	}
}
