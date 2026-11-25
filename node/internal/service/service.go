// Package service installs topcoat-node as a system service: a systemd unit
// on Linux, a Windows service (golang.org/x/sys/windows/svc/mgr) on Windows.
// Both restart the agent on failure and start it at boot.
package service

import (
	"errors"
	"io"
)

// ErrNotInstalled is returned by Restart when no service is registered (the node runs in a
// terminal or under something else).
var ErrNotInstalled = errors.New("the topcoat-node service is not installed on this machine")

// Name is the service / unit / account name.
const Name = "topcoat-node"

// InstallOptions configures Install.
type InstallOptions struct {
	// DataDir is where the service keeps its state (node key, config, models).
	DataDir string
	// Pair pairs the node into DataDir (skipped by the caller if already
	// paired). It runs after the data dir exists and before the service starts.
	Pair func(dataDir string) error
	// NoStart registers the service without starting it.
	NoStart bool
	// Account is the existing system account the node service runs as (Linux). Empty = the
	// service's own account, which the installer creates. A node that needs something the
	// account owns (the dev node's rootless Docker daemon) runs as that account.
	Account string
	Out     io.Writer
}

// UninstallOptions configures Uninstall.
type UninstallOptions struct {
	DataDir string
	// Purge also deletes the data dir (node key, downloaded models, llama.cpp).
	Purge bool
	Out   io.Writer
}

// RestartOptions configures Restart.
type RestartOptions struct {
	Out io.Writer
}
