package capability

import (
	"context"
	"encoding/json"
	"testing"
)

type echo struct{ name string }

func (e echo) Name() string                 { return e.name }
func (e echo) InputSchema() json.RawMessage { return json.RawMessage(`{"type":"object"}`) }
func (e echo) Invoke(_ context.Context, in json.RawMessage) (json.RawMessage, error) {
	return in, nil
}

func TestRegistry(t *testing.T) {
	r := NewRegistry()
	if err := r.Register(echo{"b.echo"}); err != nil {
		t.Fatal(err)
	}
	if err := r.Register(echo{"a.echo"}); err != nil {
		t.Fatal(err)
	}
	if err := r.Register(echo{"a.echo"}); err == nil {
		t.Fatal("duplicate registration accepted")
	}
	if got := r.Names(); len(got) != 2 || got[0] != "a.echo" {
		t.Fatalf("names %v", got)
	}
	c, ok := r.Get("a.echo")
	if !ok {
		t.Fatal("missing")
	}
	out, err := c.Invoke(context.Background(), json.RawMessage(`{"x":1}`))
	if err != nil || string(out) != `{"x":1}` {
		t.Fatalf("invoke %s %v", out, err)
	}
	if _, ok := r.Get("nope"); ok {
		t.Fatal("unexpected")
	}
}
