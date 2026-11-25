package servicebus

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"sync"
	"time"
)

// Handler runs one action in a service process.
type Handler func(ctx context.Context, action string, input json.RawMessage) (json.RawMessage, error)

// Client is the service's side: it connects to its socket, says which actions it has, answers
// the node's calls and may ask the node for named host functions.
type Client struct {
	service string
	actions []string
	c       net.Conn
	r       *bufio.Reader
	wmu     sync.Mutex
	pmu     sync.Mutex
	pending map[string]chan reply
	seq     int
}

// Dial connects to the socket and says hello.
func Dial(ctx context.Context, socket, service string, actions []string) (*Client, error) {
	var d net.Dialer
	c, err := d.DialContext(ctx, "unix", socket)
	if err != nil {
		return nil, err
	}
	cl := &Client{
		service: service, actions: actions, c: c, r: bufio.NewReaderSize(c, 64<<10),
		pending: map[string]chan reply{},
	}
	if err := cl.send(frame{Type: "hello", Service: service, Actions: actions}); err != nil {
		_ = c.Close()
		return nil, err
	}
	return cl, nil
}

func (cl *Client) send(f frame) error {
	b, err := json.Marshal(f)
	if err != nil {
		return err
	}
	cl.wmu.Lock()
	defer cl.wmu.Unlock()
	_ = cl.c.SetWriteDeadline(time.Now().Add(10 * time.Second))
	_, err = cl.c.Write(append(b, '\n'))
	return err
}

// Host asks the node for a host function and waits for its answer.
func (cl *Client) Host(ctx context.Context, name string, input any) (json.RawMessage, error) {
	raw, err := json.Marshal(input)
	if err != nil {
		return nil, err
	}
	cl.pmu.Lock()
	cl.seq++
	id := fmt.Sprintf("s%d", cl.seq)
	ch := make(chan reply, 1)
	cl.pending[id] = ch
	cl.pmu.Unlock()
	if err := cl.send(frame{Type: "host", ID: id, Action: name, Input: raw}); err != nil {
		return nil, err
	}
	select {
	case r := <-ch:
		return r.output, r.err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// Serve answers the node's calls with h until the connection or ctx ends. Calls run one after
// the other: a service does one thing at a time.
func (cl *Client) Serve(ctx context.Context, h Handler) error {
	defer func() { _ = cl.c.Close() }()
	go func() {
		<-ctx.Done()
		_ = cl.c.Close()
	}()
	calls := make(chan frame, 8)
	errc := make(chan error, 1)
	go func() {
		for {
			line, err := readLine(cl.r)
			if err != nil {
				errc <- err
				return
			}
			var f frame
			if json.Unmarshal(line, &f) != nil {
				errc <- errors.New("servicebus: the node sent garbage")
				return
			}
			switch f.Type {
			case "call":
				calls <- f
			case "host_result":
				cl.pmu.Lock()
				ch := cl.pending[f.ID]
				delete(cl.pending, f.ID)
				cl.pmu.Unlock()
				if ch != nil {
					if f.OK {
						ch <- reply{output: f.Output}
					} else {
						ch <- reply{err: errors.New(f.Error)}
					}
				}
			}
		}
	}()
	for {
		select {
		case f := <-calls:
			out, err := h(ctx, f.Action, f.Input)
			res := frame{Type: "result", ID: f.ID, OK: err == nil, Output: out}
			if err != nil {
				res.Error = err.Error()
			}
			if err := cl.send(res); err != nil {
				return err
			}
		case err := <-errc:
			return err
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}
