// Package servicebus is the local transport between a node and its services (NS-01, D80).
//
// A node is a machine; what runs on it are services (build, later others), each its own process
// under its own OS user. The node listens on one Unix socket per service in a root-owned
// directory; the socket is owned by the service's OS user and the node identifies the caller by
// the kernel (SO_PEERCRED), so no shared secret exists and a service cannot pose as another.
// The node opens no network port. The whitelist of actions is per service: the node refuses a
// call to an action the service did not declare and the config did not allow, and a service can
// ask the node for a handful of named things only (HostFunc), never for a shell.
//
// The wire is one JSON object per line, in both directions:
//
//	service -> node  {"type":"hello","service":"build","actions":["build.fetch",...]}
//	node -> service  {"type":"call","id":"1","action":"build.fetch","input":{...}}
//	service -> node  {"type":"result","id":"1","ok":true,"output":{...}} | {"ok":false,"error":"..."}
//	service -> node  {"type":"host","id":"s1","action":"upload","input":{...}}
//	node -> service  {"type":"host_result","id":"s1","ok":true,"output":{...}}
package servicebus

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"sync/atomic"
	"time"
)

// MaxFrame bounds one line on the wire.
const MaxFrame = 4 << 20

var (
	serviceRe = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}$`)
	actionRe  = regexp.MustCompile(`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`)
	hostRe    = regexp.MustCompile(`^[a-z][a-z0-9_]{0,31}$`)
)

// Errors a caller can test for.
var (
	ErrNotConnected = errors.New("servicebus: the service is not connected")
	ErrNotAllowed   = errors.New("servicebus: the action is not allowed for this service")
	ErrClosed       = errors.New("servicebus: the connection closed")
)

// Spec says what one service may do: the actions the node may call on it.
type Spec struct {
	ID      string
	Actions []string
}

// Resolver maps the OS user id of a socket peer to the service it may be.
type Resolver func(uid uint32) (service string, ok bool)

// HostFunc is something a service may ask the node for (for example to upload a file with the
// node's credentials). It gets the id of the service that asked.
type HostFunc func(ctx context.Context, service string, input json.RawMessage) (json.RawMessage, error)

type frame struct {
	Type    string          `json:"type"`
	ID      string          `json:"id,omitempty"`
	Service string          `json:"service,omitempty"`
	Actions []string        `json:"actions,omitempty"`
	Action  string          `json:"action,omitempty"`
	Input   json.RawMessage `json:"input,omitempty"`
	OK      bool            `json:"ok,omitempty"`
	Output  json.RawMessage `json:"output,omitempty"`
	Error   string          `json:"error,omitempty"`
}

type reply struct {
	output json.RawMessage
	err    error
}

type conn struct {
	service string
	c       net.Conn
	wmu     sync.Mutex
	pmu     sync.Mutex
	pending map[string]chan reply
	done    chan struct{}
}

func (c *conn) send(f frame) error {
	b, err := json.Marshal(f)
	if err != nil {
		return err
	}
	c.wmu.Lock()
	defer c.wmu.Unlock()
	_ = c.c.SetWriteDeadline(time.Now().Add(10 * time.Second))
	_, err = c.c.Write(append(b, '\n'))
	return err
}

// Bus is the node's side.
type Bus struct {
	dir     string
	specs   map[string]Spec
	resolve Resolver
	peer    func(net.Conn) (uint32, error)

	mu    sync.Mutex
	conns map[string]*conn
	host  map[string]HostFunc
	seq   atomic.Uint64
}

// New returns a bus that will listen below dir for the given services.
func New(dir string, specs []Spec, resolve Resolver) (*Bus, error) {
	b := &Bus{
		dir: dir, specs: map[string]Spec{}, resolve: resolve, peer: PeerUID,
		conns: map[string]*conn{}, host: map[string]HostFunc{},
	}
	for _, s := range specs {
		if !serviceRe.MatchString(s.ID) {
			return nil, fmt.Errorf("servicebus: %q is not a service name", s.ID)
		}
		for _, a := range s.Actions {
			if !actionRe.MatchString(a) {
				return nil, fmt.Errorf("servicebus: %q is not an action name", a)
			}
		}
		b.specs[s.ID] = s
	}
	return b, nil
}

// Handle registers what services may ask of the node.
func (b *Bus) Handle(name string, f HostFunc) error {
	if !hostRe.MatchString(name) {
		return fmt.Errorf("servicebus: %q is not a host function name", name)
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.host[name] = f
	return nil
}

// SocketPath is where the service of that id connects.
func (b *Bus) SocketPath(service string) string { return filepath.Join(b.dir, service+".sock") }

// Connected reports whether the service has said hello and is still there.
func (b *Bus) Connected(service string) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	_, ok := b.conns[service]
	return ok
}

// Listen opens the sockets and serves until ctx ends. ownerOf, when not nil, gives the OS user
// and group that own each socket (the service's own user).
func (b *Bus) Listen(ctx context.Context, chown func(path, service string) error) error {
	if err := os.MkdirAll(b.dir, 0o750); err != nil {
		return err
	}
	var lns []net.Listener
	for id := range b.specs {
		path := b.SocketPath(id)
		_ = os.Remove(path)
		ln, err := net.Listen("unix", path)
		if err != nil {
			for _, l := range lns {
				_ = l.Close()
			}
			return fmt.Errorf("servicebus: listen %s: %w", path, err)
		}
		if err := os.Chmod(path, 0o600); err != nil {
			_ = ln.Close()
			return err
		}
		if chown != nil {
			if err := chown(path, id); err != nil {
				_ = ln.Close()
				return fmt.Errorf("servicebus: give %s to its user: %w", path, err)
			}
		}
		lns = append(lns, ln)
		go b.accept(ctx, ln, id)
	}
	go func() {
		<-ctx.Done()
		for _, l := range lns {
			_ = l.Close()
		}
	}()
	return nil
}

func (b *Bus) accept(ctx context.Context, ln net.Listener, id string) {
	for {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		go b.serve(ctx, c, id)
	}
}

func (b *Bus) serve(ctx context.Context, c net.Conn, socketService string) {
	defer func() { _ = c.Close() }()
	uid, err := b.peer(c)
	if err != nil {
		return
	}
	who, ok := b.resolve(uid)
	if !ok || who != socketService {
		return // the kernel says it is somebody else: no word is read from it
	}
	r := bufio.NewReaderSize(c, 64<<10)
	_ = c.SetReadDeadline(time.Now().Add(10 * time.Second))
	line, err := readLine(r)
	if err != nil {
		return
	}
	_ = c.SetReadDeadline(time.Time{})
	var hello frame
	if json.Unmarshal(line, &hello) != nil || hello.Type != "hello" || hello.Service != who {
		return
	}
	spec := b.specs[who]
	for _, a := range hello.Actions { // it may declare less than the config allows, never more
		if !contains(spec.Actions, a) {
			return
		}
	}
	cn := &conn{service: who, c: c, pending: map[string]chan reply{}, done: make(chan struct{})}
	b.mu.Lock()
	if old := b.conns[who]; old != nil {
		_ = old.c.Close() // one connection per service: a new one replaces the old
	}
	b.conns[who] = cn
	b.mu.Unlock()
	defer func() {
		close(cn.done)
		b.mu.Lock()
		if b.conns[who] == cn {
			delete(b.conns, who)
		}
		b.mu.Unlock()
	}()
	for {
		line, err := readLine(r)
		if err != nil {
			return
		}
		var f frame
		if json.Unmarshal(line, &f) != nil {
			return
		}
		switch f.Type {
		case "result":
			cn.pmu.Lock()
			ch := cn.pending[f.ID]
			delete(cn.pending, f.ID)
			cn.pmu.Unlock()
			if ch != nil {
				if f.OK {
					ch <- reply{output: f.Output}
				} else {
					ch <- reply{err: errors.New(f.Error)}
				}
			}
		case "host":
			go b.answerHost(ctx, cn, f)
		default:
			return
		}
	}
}

func (b *Bus) answerHost(ctx context.Context, cn *conn, f frame) {
	b.mu.Lock()
	h := b.host[f.Action]
	b.mu.Unlock()
	if h == nil {
		_ = cn.send(frame{Type: "host_result", ID: f.ID, Error: "unknown host function"})
		return
	}
	out, err := h(ctx, cn.service, f.Input)
	if err != nil {
		_ = cn.send(frame{Type: "host_result", ID: f.ID, Error: err.Error()})
		return
	}
	_ = cn.send(frame{Type: "host_result", ID: f.ID, OK: true, Output: out})
}

// Call runs one action on a service and waits for its answer.
func (b *Bus) Call(ctx context.Context, service, action string, input json.RawMessage) (json.RawMessage, error) {
	spec, ok := b.specs[service]
	if !ok || !contains(spec.Actions, action) {
		return nil, fmt.Errorf("%w: %s on %s", ErrNotAllowed, action, service)
	}
	b.mu.Lock()
	cn := b.conns[service]
	b.mu.Unlock()
	if cn == nil {
		return nil, ErrNotConnected
	}
	id := fmt.Sprint(b.seq.Add(1))
	ch := make(chan reply, 1)
	cn.pmu.Lock()
	cn.pending[id] = ch
	cn.pmu.Unlock()
	drop := func() {
		cn.pmu.Lock()
		delete(cn.pending, id)
		cn.pmu.Unlock()
	}
	if err := cn.send(frame{Type: "call", ID: id, Action: action, Input: input}); err != nil {
		drop()
		return nil, err
	}
	select {
	case r := <-ch:
		return r.output, r.err
	case <-cn.done:
		drop()
		return nil, ErrClosed
	case <-ctx.Done():
		drop()
		return nil, ctx.Err()
	}
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func readLine(r *bufio.Reader) ([]byte, error) {
	var out []byte
	for {
		chunk, isPrefix, err := r.ReadLine()
		if err != nil {
			return nil, err
		}
		out = append(out, chunk...)
		if len(out) > MaxFrame {
			return nil, errors.New("servicebus: frame too large")
		}
		if !isPrefix {
			return out, nil
		}
	}
}
