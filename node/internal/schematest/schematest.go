// Package schematest validates JSON against the proto/v1 schemas.
//
// TEST-ONLY: this package must only be imported from _test.go files so that
// the JSON Schema library never lands in the topcoat-node binary (verified
// with `go version -m bin/topcoat-node-*`).
package schematest

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

// BaseID is the $id prefix of the current (v1) schemas.
const BaseID = "https://sloppy-topcoat.example/proto/v1/"

var (
	once     sync.Once
	compiled map[string]*jsonschema.Schema
	loadErr  error
)

// RepoRoot returns the repository root (the directory containing proto/).
func RepoRoot() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Clean(filepath.Join(filepath.Dir(file), "..", "..", ".."))
}

// ProtoDir returns <repo>/proto.
func ProtoDir() string { return filepath.Join(RepoRoot(), "proto") }

func load() {
	dir := filepath.Join(ProtoDir(), "v1")
	entries, err := os.ReadDir(dir)
	if err != nil {
		loadErr = fmt.Errorf("read schema dir: %w", err)
		return
	}
	c := jsonschema.NewCompiler()
	c.AssertFormat()
	var names []string
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".schema.json") {
			continue
		}
		f, err := os.Open(filepath.Join(dir, e.Name()))
		if err != nil {
			loadErr = err
			return
		}
		doc, err := jsonschema.UnmarshalJSON(f)
		_ = f.Close()
		if err != nil {
			loadErr = fmt.Errorf("parse %s: %w", e.Name(), err)
			return
		}
		if err := c.AddResource(BaseID+e.Name(), doc); err != nil {
			loadErr = fmt.Errorf("add %s: %w", e.Name(), err)
			return
		}
		names = append(names, e.Name())
	}
	compiled = map[string]*jsonschema.Schema{}
	for _, n := range names {
		s, err := c.Compile(BaseID + n)
		if err != nil {
			loadErr = fmt.Errorf("compile %s: %w", n, err)
			return
		}
		compiled[strings.TrimSuffix(n, ".schema.json")] = s
	}
}

// Validate validates raw JSON against <name>.schema.json (name e.g. "hello").
func Validate(name string, raw []byte) error {
	once.Do(load)
	if loadErr != nil {
		return loadErr
	}
	s, ok := compiled[name]
	if !ok {
		return fmt.Errorf("no schema %q", name)
	}
	inst, err := jsonschema.UnmarshalJSON(strings.NewReader(string(raw)))
	if err != nil {
		return fmt.Errorf("parse instance: %w", err)
	}
	return s.Validate(inst)
}

// ValidateValue marshals v and validates it against <name>.schema.json.
func ValidateValue(name string, v any) error {
	raw, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return Validate(name, raw)
}
