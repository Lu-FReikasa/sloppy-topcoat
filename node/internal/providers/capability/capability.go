// Package capability defines named local tools the hub can invoke (stage 2+).
//
// There is deliberately no generic shell: each capability is a fixed, named
// operation with a JSON-Schema-described input, assigned per node by the hub.
package capability

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"sync"
)

// Capability is a named local tool.
type Capability interface {
	Name() string
	// InputSchema is the JSON Schema of the Invoke input.
	InputSchema() json.RawMessage
	Invoke(ctx context.Context, input json.RawMessage) (json.RawMessage, error)
}

// Info is what the hub and the assistant learn about a capability beyond its name and schema.
type Info struct {
	// Description says in one or two sentences what the capability returns.
	Description string
	// ReadOnly is true for a capability that changes nothing on the machine. Every capability
	// of E2-04 is read-only; the hub executes only those without a confirmation (D15).
	ReadOnly bool
	// Scratch is true for a capability of a dev node that changes only the node's own scratch
	// area (the workshop worktrees, D52): disposable, shown as a diff, so it runs without a
	// confirmation. It is never true together with ReadOnly.
	Scratch bool
	// Manage is true for a capability the person runs from the hub's own page (create a local
	// repository, merge a branch into the default branch, publish it): it is run only by the
	// action workshop.manage, never by capability.invoke or capability.execute, so an assistant
	// can never reach it. Never true together with ReadOnly or Scratch.
	Manage bool
}

// Describer is implemented by capabilities that describe themselves. A capability that does not
// is listed without a description and as *not* read-only, so the safe default wins.
type Describer interface {
	Info() Info
}

// InfoOf returns the description of a capability (see Describer).
func InfoOf(c Capability) Info {
	if d, ok := c.(Describer); ok {
		return d.Info()
	}
	return Info{}
}

// Registry holds the capabilities compiled into this binary.
type Registry struct {
	mu   sync.RWMutex
	caps map[string]Capability
}

// NewRegistry returns an empty registry.
func NewRegistry() *Registry { return &Registry{caps: map[string]Capability{}} }

// Register adds a capability; names must be unique.
func (r *Registry) Register(c Capability) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.caps[c.Name()]; ok {
		return fmt.Errorf("capability %q already registered", c.Name())
	}
	r.caps[c.Name()] = c
	return nil
}

// Get returns a capability by name.
func (r *Registry) Get(name string) (Capability, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	c, ok := r.caps[name]
	return c, ok
}

// Names returns the registered names, sorted.
func (r *Registry) Names() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]string, 0, len(r.caps))
	for n := range r.caps {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}
