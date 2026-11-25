package tools

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"time"
)

// Ping without a privilege (E3-04 fix): net.check sends ICMP echo requests over an unprivileged
// ICMP datagram socket (Linux: socket(AF_INET, SOCK_DGRAM, IPPROTO_ICMP)), which the kernel allows
// for the groups in net.ipv4.ping_group_range. The ping binary needs cap_net_raw (or the same
// sysctl) and fails under a service account — a failure to *send* says nothing about the target.

const (
	pingCount   = 3
	pingTimeout = 2 * time.Second // wait per packet
)

// PingResult is the outcome of the echo requests to one host.
type PingResult struct {
	Sent, Received int
	AvgMS          float64
}

// errPingNotPermitted is returned when this process may not open an ICMP socket.
var errPingNotPermitted = errors.New("ping is not permitted for this account (net.ipv4.ping_group_range)")

// errPingUnsupported is returned on systems without the unprivileged socket.
var errPingUnsupported = errors.New("ping is not supported on this system")

// icmpEcho builds an ICMP echo request (type 8) with the identifier, sequence number and a payload.
func icmpEcho(id, seq uint16, payload []byte) []byte {
	b := make([]byte, 8+len(payload))
	b[0] = 8 // echo request
	binary.BigEndian.PutUint16(b[4:], id)
	binary.BigEndian.PutUint16(b[6:], seq)
	copy(b[8:], payload)
	binary.BigEndian.PutUint16(b[2:], icmpChecksum(b))
	return b
}

// icmpChecksum is the Internet checksum (RFC 1071) of b with its checksum field zero.
func icmpChecksum(b []byte) uint16 {
	var sum uint32
	for i := 0; i+1 < len(b); i += 2 {
		sum += uint32(b[i])<<8 | uint32(b[i+1])
	}
	if len(b)%2 == 1 {
		sum += uint32(b[len(b)-1]) << 8
	}
	for sum>>16 != 0 {
		sum = sum&0xffff + sum>>16
	}
	return ^uint16(sum)
}

// icmpEchoReplySeq returns the sequence number if b is an ICMP echo reply (type 0), whether or not
// it still has the IP header in front (raw sockets deliver it, datagram sockets do not).
func icmpEchoReplySeq(b []byte) (uint16, bool) {
	if len(b) >= 20 && b[0]>>4 == 4 { // an IPv4 header
		ihl := int(b[0]&0x0f) * 4
		if ihl < 20 || len(b) < ihl+8 {
			return 0, false
		}
		b = b[ihl:]
	}
	if len(b) < 8 || b[0] != 0 || b[1] != 0 {
		return 0, false
	}
	return binary.BigEndian.Uint16(b[6:]), true
}

// resolveV4 picks an IPv4 address of host.
func resolveV4(ctx context.Context, host string) (net.IP, error) {
	if ip := net.ParseIP(host); ip != nil {
		if v4 := ip.To4(); v4 != nil {
			return v4, nil
		}
		return nil, fmt.Errorf("%s is not an IPv4 address (ping is IPv4 only)", host)
	}
	addrs, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		return nil, errors.New("does not resolve")
	}
	for _, a := range addrs {
		if v4 := a.IP.To4(); v4 != nil {
			return v4, nil
		}
	}
	return nil, errors.New("no IPv4 address (ping is IPv4 only)")
}
