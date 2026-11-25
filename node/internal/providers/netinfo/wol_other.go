//go:build !linux

package netinfo

func platformWoL() func(string) (bool, bool) { return nil }
