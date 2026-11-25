package netinfo

import (
	"net"
	"net/netip"
	"sort"
	"strings"
)

// maxAddresses is the schema limit of hardware.addresses.
const maxAddresses = 16

// containerHints name interfaces whose addresses are only reachable from the same machine
// (container bridges, VM host-only switches). VPN and overlay interfaces are kept on purpose:
// they are often the only route between two nodes.
var containerHints = []string{"docker", "veth", "br-", "virbr", "vboxnet", "vmnet", "vethernet", "lxcbr", "cni", "flannel", "podman"}

// ifaceAddrs is one interface with its addresses.
type ifaceAddrs struct {
	Name  string
	Flags net.Flags
	Addrs []net.Addr
}

// Addresses lists the unicast IPs of this machine (no loopback, no link-local, no container
// bridges), IPv4 first: the candidates for a direct model transfer (E2-07).
func Addresses() []string {
	ifs, err := net.Interfaces()
	if err != nil {
		return nil
	}
	list := make([]ifaceAddrs, 0, len(ifs))
	for _, i := range ifs {
		addrs, err := i.Addrs()
		if err != nil {
			continue
		}
		list = append(list, ifaceAddrs{Name: i.Name, Flags: i.Flags, Addrs: addrs})
	}
	return addressesOf(list)
}

func addressesOf(list []ifaceAddrs) []string {
	var v4, v6 []string
	seen := map[string]bool{}
	for _, i := range list {
		if i.Flags&net.FlagUp == 0 || i.Flags&net.FlagLoopback != 0 || isContainerBridge(i.Name) {
			continue
		}
		for _, a := range i.Addrs {
			var ip net.IP
			switch v := a.(type) {
			case *net.IPNet:
				ip = v.IP
			case *net.IPAddr:
				ip = v.IP
			}
			addr, ok := netip.AddrFromSlice(ip)
			if !ok {
				continue
			}
			addr = addr.Unmap()
			if !addr.IsValid() || addr.IsLoopback() || addr.IsLinkLocalUnicast() || addr.IsMulticast() || addr.IsUnspecified() {
				continue
			}
			s := addr.String()
			if seen[s] {
				continue
			}
			seen[s] = true
			if addr.Is4() {
				v4 = append(v4, s)
			} else {
				v6 = append(v6, s)
			}
		}
	}
	sort.Strings(v4)
	sort.Strings(v6)
	out := append(v4, v6...)
	if len(out) > maxAddresses {
		out = out[:maxAddresses]
	}
	return out
}

func isContainerBridge(name string) bool {
	n := strings.ToLower(name)
	for _, h := range containerHints {
		if strings.HasPrefix(n, h) {
			return true
		}
	}
	return false
}
