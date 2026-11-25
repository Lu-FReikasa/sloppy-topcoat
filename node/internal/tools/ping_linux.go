//go:build linux

package tools

import (
	"context"
	"errors"
	"os"
	"time"

	"golang.org/x/sys/unix"
)

// pingHostICMP sends pingCount echo requests to host over an unprivileged ICMP datagram socket.
// It returns errPingNotPermitted when the account may not open one.
func pingHostICMP(ctx context.Context, host string) (PingResult, error) {
	ip, err := resolveV4(ctx, host)
	if err != nil {
		return PingResult{}, err
	}
	fd, err := unix.Socket(unix.AF_INET, unix.SOCK_DGRAM|unix.SOCK_CLOEXEC, unix.IPPROTO_ICMP)
	if err != nil {
		switch {
		case errors.Is(err, unix.EACCES), errors.Is(err, unix.EPERM):
			return PingResult{}, errPingNotPermitted
		case errors.Is(err, unix.EAFNOSUPPORT), errors.Is(err, unix.EPROTONOSUPPORT):
			return PingResult{}, errPingUnsupported
		}
		return PingResult{}, os.NewSyscallError("socket", err)
	}
	defer unix.Close(fd) //nolint:errcheck // a closed probe socket
	var dst [4]byte
	copy(dst[:], ip)
	to := &unix.SockaddrInet4{Addr: dst}
	res := PingResult{}
	var total time.Duration
	buf := make([]byte, 1500)
	for seq := 1; seq <= pingCount; seq++ {
		if ctx.Err() != nil {
			break
		}
		// the kernel sets the identifier of a datagram ICMP socket itself
		if err := unix.Sendto(fd, icmpEcho(0, uint16(seq), []byte("topcoat-ping")), 0, to); err != nil {
			if errors.Is(err, unix.EACCES) || errors.Is(err, unix.EPERM) {
				return res, errPingNotPermitted
			}
			continue // unreachable, no route …: counted as lost
		}
		res.Sent++
		start := time.Now()
		deadline := start.Add(pingTimeout)
		for time.Until(deadline) > 0 && ctx.Err() == nil {
			left := time.Until(deadline)
			if left > 200*time.Millisecond {
				left = 200 * time.Millisecond // look at ctx now and then
			}
			tv := unix.NsecToTimeval(left.Nanoseconds())
			_ = unix.SetsockoptTimeval(fd, unix.SOL_SOCKET, unix.SO_RCVTIMEO, &tv)
			n, _, err := unix.Recvfrom(fd, buf, 0)
			if err != nil {
				continue // timeout, interrupted: try until the deadline
			}
			if got, ok := icmpEchoReplySeq(buf[:n]); ok && int(got) == seq {
				res.Received++
				total += time.Since(start)
				break
			}
		}
	}
	if res.Received > 0 {
		res.AvgMS = float64(total.Microseconds()) / 1000 / float64(res.Received)
	}
	return res, nil
}
