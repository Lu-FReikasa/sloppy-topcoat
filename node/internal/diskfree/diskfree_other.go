//go:build !linux && !windows

package diskfree

import "errors"

func usage(string) (Usage, error) {
	return Usage{}, errors.New("disk space is not measured on this platform")
}
