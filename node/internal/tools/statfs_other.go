//go:build !linux

package tools

import "errors"

func statFS(string) (FSStat, error) {
	return FSStat{}, errors.New("disk usage is only available on Linux nodes")
}
