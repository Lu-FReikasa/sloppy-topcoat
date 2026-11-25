//go:build !linux && !windows

package service

import (
	"context"
	"errors"

	"topcoat/node/internal/update"
)

var errUnsupported = errors.New("service installation is only supported on Linux (systemd) and Windows")

// Install is not supported on this OS.
func Install(InstallOptions) error { return errUnsupported }

// Uninstall is not supported on this OS.
func Uninstall(UninstallOptions) error { return errUnsupported }

// EnsureUpdaterRestarts is a Windows matter (systemd restarts the updater by its unit's Restart=).
func EnsureUpdaterRestarts() error { return nil }

// IsWindowsService is false on this OS.
func IsWindowsService() bool { return false }

// RunWindowsService is only used on Windows.
func RunWindowsService(string, func(stop <-chan struct{}) int) int { return 1 }

// Restart is not supported on this OS.
func Restart(RestartOptions) error { return errUnsupported }

// NewHost is not supported on this OS.
func NewHost() update.Host { return unsupportedHost{} }

type unsupportedHost struct{}

func (unsupportedHost) StopNode(context.Context) error               { return errUnsupported }
func (unsupportedHost) StartNode(context.Context) error              { return errUnsupported }
func (unsupportedHost) ReplaceProgram(context.Context, []byte) error { return errUnsupported }
func (unsupportedHost) RestoreProgram(context.Context) error         { return errUnsupported }
