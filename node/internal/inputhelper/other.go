//go:build !windows

package inputhelper

import (
	"errors"
	"time"
)

var errWindowsOnly = errors.New("the input helper exists on Windows only")

// Run is only meaningful on Windows.
func Run(string, func(string, ...any)) error { return errWindowsOnly }

// IdleFromHelper is only meaningful on Windows.
func IdleFromHelper(uint32) (time.Duration, error) { return 0, errWindowsOnly }
