//go:build !linux

package servicebus

import (
	"errors"
	"net"
)

// PeerUID is not available here: the build service is Linux-only first (NS-01); a named pipe
// transport for Windows can come later. Nothing connects, so nothing is trusted.
func PeerUID(net.Conn) (uint32, error) {
	return 0, errors.New("servicebus: peer credentials are only available on Linux")
}
