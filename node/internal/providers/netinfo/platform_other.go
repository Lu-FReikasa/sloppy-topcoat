//go:build !windows

package netinfo

func platformWindowsAdapters() func() []windowsAdapter { return nil }
