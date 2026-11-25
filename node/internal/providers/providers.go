// Package providers holds what the hardware / runtime provider packages share.
//
// Each provider (gpu, runner, watcher, capability, sysinfo) is an interface with
// a fake implementation in package fake (used by tests and `run --fake`) and
// real implementations that are stubs until they are verified on real hardware.
package providers

import "errors"

// ErrNotImplemented is returned by provider stubs that are not implemented yet.
var ErrNotImplemented = errors.New("not implemented")
