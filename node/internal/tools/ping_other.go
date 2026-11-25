//go:build !linux

package tools

import "context"

// pingHostICMP: no unprivileged ICMP socket here (UNVERIFIED on other systems: they could use the
// ping program or a platform API); net.check says so instead of guessing.
func pingHostICMP(context.Context, string) (PingResult, error) {
	return PingResult{}, errPingUnsupported
}
